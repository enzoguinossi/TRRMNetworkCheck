"""
net_utilization.py - Monitor de Utilizacao de Rede para Tactical RMM

Uso:
    python net_utilization.py                   # interface padrao: Ethernet
    python net_utilization.py "Ethernet 3"      # interface customizada

Codigos de saida (exit codes):
    0 -> OK       (utilizacao < 75%)
    1 -> WARNING  (utilizacao >= 75%)
    2 -> CRITICAL (utilizacao = 100%)
    3 -> UNKNOWN  (erro de coleta ou interface nao encontrada)
"""

import subprocess
import sys
import re
import time


# ==============================================================================
# Camada de execucao — responsavel por rodar comandos PowerShell
# ==============================================================================

class ExecutorPowerShell:
    """
    Responsavel por executar comandos PowerShell e retornar a saida como string.

    Encapsula a chamada ao subprocess, garantindo encoding correto
    e tratamento de erros de forma centralizada.
    """

    def executar(self, comando: str) -> str:
        """
        Executa um comando PowerShell e retorna o stdout como string.

        Utiliza as flags -NoProfile e -NonInteractive para evitar
        carregamento desnecessario de perfis e prompts interativos.

        Parametros
        ----------
        comando : str
            Comando PowerShell a ser executado.

        Retorna
        -------
        str
            Saida padrao (stdout) do comando, sem espacos nas bordas.
            Retorna string vazia em caso de excecao.
        """
        try:
            resultado = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", comando],
                capture_output=True,
                encoding="utf-8",
                errors="replace"
            )
            return resultado.stdout.strip()
        except Exception:
            return ""


# ==============================================================================
# Camada de acesso a dados — coleta informacoes da NIC via PowerShell
# ==============================================================================

class ColetorNIC:
    """
    Responsavel por coletar informacoes e estatisticas de uma interface de rede.

    Utiliza Get-NetAdapter para link speed e InterfaceDescription, e
    Win32_PerfRawData_Tcpip_NetworkInterface para os contadores brutos
    de bytes acumulados.

    Ao contrario do Win32_PerfFormattedData (que retorna uma taxa pre-calculada
    pelo WMI com janela de 1 segundo e pode zerar entre execucoes), o
    Win32_PerfRawData retorna contadores cumulativos desde a inicializacao
    da interface. Dois snapshots com intervalo controlado garantem uma
    medicao precisa independente do ciclo interno do WMI.

    Parametros
    ----------
    interface : str
        Nome amigavel da interface de rede (ex: "Ethernet", "Ethernet 3").
    executor : ExecutorPowerShell
        Instancia do executor de comandos PowerShell.
    """

    def __init__(self, interface: str, executor: ExecutorPowerShell) -> None:
        """
        Inicializa o coletor com o nome da interface e o executor PowerShell.

        Parametros
        ----------
        interface : str
            Nome amigavel da interface de rede a ser monitorada.
        executor : ExecutorPowerShell
            Instancia responsavel por executar os comandos PowerShell.
        """
        self.interface  = interface
        self._executor  = executor
        self._wmi_name: str = ""

    def obter_link_speed_bps(self) -> int:
        """
        Retorna a velocidade maxima (link speed) da interface em bits por segundo.

        Utiliza Get-NetAdapter, que e a unica fonte confiavel no Windows PT-BR,
        pois o netsh nao exibe velocidade em portugues e o wmic retorna Speed
        vazio em algumas NICs (ex: Intel I219).

        Retorna
        -------
        int
            Velocidade do link em bits/s (ex: 100_000_000 para Fast Ethernet).
            Retorna 0 se a interface nao for encontrada ou nao tiver link speed.
        """
        saida = self._executor.executar(
            f"(Get-NetAdapter -Name '{self.interface}' -ErrorAction SilentlyContinue).LinkSpeed"
        )
        m = re.search(r"([\d\.]+)\s*(Gbps|Mbps|Kbps|bps)", saida, re.IGNORECASE)
        if m:
            num  = float(m.group(1))
            unit = m.group(2).lower()
            mult = {"gbps": 1_000_000_000, "mbps": 1_000_000, "kbps": 1_000, "bps": 1}
            return int(num * mult.get(unit, 1))
        return 0

    def listar_interfaces(self) -> list:
        """
        Retorna a lista de nomes de todas as interfaces de rede disponiveis.

        Utilizado para exibir opcoes validas quando a interface informada
        nao for encontrada.

        Retorna
        -------
        list of str
            Lista com os nomes amigaveis de todas as interfaces.
            Retorna lista vazia se nenhuma interface for encontrada.
        """
        saida = self._executor.executar(
            "Get-NetAdapter | Select-Object -ExpandProperty Name"
        )
        return [l.strip() for l in saida.splitlines() if l.strip()]

    def _obter_descricao(self) -> str:
        """
        Retorna a InterfaceDescription da NIC pelo nome amigavel.

        A InterfaceDescription e usada para associar a NIC aos contadores
        WMI, cujos nomes de instancia substituem caracteres especiais
        por underscores.

        Retorna
        -------
        str
            Descricao do hardware (ex: "Realtek RTL8139/810x Family Fast Ethernet NIC").
            Retorna string vazia se nao encontrada.
        """
        return self._executor.executar(
            f"(Get-NetAdapter -Name '{self.interface}' "
            f"-ErrorAction SilentlyContinue).InterfaceDescription"
        )

    def _resolver_nome_wmi(self) -> str:
        """
        Resolve o nome de instancia WMI correspondente a interface.

        O WMI armazena instancias com o nome da InterfaceDescription, mas
        substituindo caracteres especiais por underscores. Este metodo lista
        todas as instancias de Win32_PerfRawData_Tcpip_NetworkInterface e
        encontra a que melhor corresponde a descricao da NIC alvo.

        O resultado e cacheado em self._wmi_name para evitar consultas
        repetidas entre os dois snapshots de coleta.

        Retorna
        -------
        str
            Nome exato da instancia WMI.
            Retorna string vazia se nenhuma instancia for encontrada.
        """
        if self._wmi_name:
            return self._wmi_name

        desc = self._obter_descricao()

        saida = self._executor.executar(
            "Get-WmiObject -Class Win32_PerfRawData_Tcpip_NetworkInterface "
            "| Select-Object -ExpandProperty Name"
        )
        instancias = [l.strip() for l in saida.splitlines() if l.strip()]

        if not instancias:
            return ""

        def normalizar(s: str) -> str:
            """Converte para minusculas e substitui nao-alfanumericos por ponto."""
            return re.sub(r"[^a-z0-9]", ".", s.lower())

        # 1) Match exato apos normalizacao
        if desc:
            desc_norm = normalizar(desc)
            for inst in instancias:
                if normalizar(inst) == desc_norm:
                    self._wmi_name = inst
                    return self._wmi_name

        # 2) Match pelos primeiros 20 caracteres normalizados da descricao
        if desc:
            prefixo = normalizar(desc)[:20]
            for inst in instancias:
                if normalizar(inst).startswith(prefixo):
                    self._wmi_name = inst
                    return self._wmi_name

        # 3) Match pelo nome amigavel da interface
        iface_norm = normalizar(self.interface)
        for inst in instancias:
            if iface_norm in normalizar(inst):
                self._wmi_name = inst
                return self._wmi_name

        # 4) Primeira instancia como ultimo recurso
        self._wmi_name = instancias[0]
        return self._wmi_name

    def obter_bytes_acumulados(self) -> tuple:
        """
        Retorna os bytes acumulados de recepcao e envio desde a inicializacao
        da interface.

        Utiliza Win32_PerfRawData_Tcpip_NetworkInterface, que armazena
        contadores cumulativos. Para obter a taxa atual, este metodo deve
        ser chamado duas vezes com um intervalo de tempo entre as chamadas,
        e o delta dividido pelo intervalo (ver MonitorRede.executar).

        Retorna
        -------
        tuple of (int, int)
            Par (bytes_recebidos_acumulados, bytes_enviados_acumulados).
            Retorna (-1, -1) se a coleta falhar.
        """
        nome_wmi = self._resolver_nome_wmi()
        if not nome_wmi:
            return -1, -1

        nome_escapado = nome_wmi.replace("'", "''")
        saida = self._executor.executar(
            f"$n = Get-WmiObject -Class Win32_PerfRawData_Tcpip_NetworkInterface "
            f"-Filter \"Name='{nome_escapado}'\" -ErrorAction SilentlyContinue; "
            f"if ($n) {{ Write-Output \"$($n.BytesReceivedPerSec),$($n.BytesSentPerSec)\" }}"
        )
        m = re.match(r"^(\d+),(\d+)$", saida.strip())
        if m:
            return int(m.group(1)), int(m.group(2))
        return -1, -1


# ==============================================================================
# Camada de calculo — processa os snapshots e calcula percentuais
# ==============================================================================

class CalculadorUtilizacao:
    """
    Responsavel por calcular os percentuais de utilizacao da interface
    com base em dois snapshots de bytes acumulados e no intervalo de tempo.

    O calculo e identico ao utilizado pelo Task Manager do Windows:
        taxa_bits_por_segundo = (delta_bytes / delta_segundos) * 8
        percentual = (taxa_bits / link_speed_bits) * 100

    Parametros
    ----------
    link_speed_bps : int
        Velocidade maxima do link em bits por segundo.
    """

    def __init__(self, link_speed_bps: int) -> None:
        """
        Inicializa o calculador com a velocidade maxima do link.

        Parametros
        ----------
        link_speed_bps : int
            Velocidade maxima da interface em bits por segundo.
        """
        self.link_speed_bps = link_speed_bps

    def calcular(self, snap1: tuple, snap2: tuple, delta_segundos: float) -> dict:
        """
        Calcula download, upload e utilizacao total a partir de dois snapshots.

        Parametros
        ----------
        snap1 : tuple of (int, int)
            Primeiro snapshot (bytes_recebidos, bytes_enviados) acumulados.
        snap2 : tuple of (int, int)
            Segundo snapshot coletado apos delta_segundos.
        delta_segundos : float
            Intervalo de tempo real entre os dois snapshots em segundos.

        Retorna
        -------
        dict com as chaves:
            download_bps  : float  — taxa de download em bits/s
            upload_bps    : float  — taxa de upload em bits/s
            download_pct  : float  — percentual de download (0-100)
            upload_pct    : float  — percentual de upload (0-100)
            total_pct     : float  — percentual total, limitado a 100%
        """
        if delta_segundos <= 0:
            delta_segundos = 1.0

        download_bps = max((snap2[0] - snap1[0]) / delta_segundos * 8, 0.0)
        upload_bps   = max((snap2[1] - snap1[1]) / delta_segundos * 8, 0.0)

        download_pct = min(round((download_bps / self.link_speed_bps) * 100, 2), 100.0)
        upload_pct   = min(round((upload_bps   / self.link_speed_bps) * 100, 2), 100.0)
        total_pct    = min(round(download_pct + upload_pct, 2), 100.0)

        return {
            "download_bps" : download_bps,
            "upload_bps"   : upload_bps,
            "download_pct" : download_pct,
            "upload_pct"   : upload_pct,
            "total_pct"    : total_pct,
        }


# ==============================================================================
# Camada de apresentacao — formata e exibe os resultados para o TRMM
# ==============================================================================

class ApresentadorTRMM:
    """
    Responsavel por formatar e exibir os resultados no formato esperado
    pelo Tactical RMM (Nagios perfdata).

    O bloco apos o pipe '|' na primeira linha e interpretado pelo TRMM
    para gerar graficos automaticos de series temporais.

    Parametros
    ----------
    warn_pct : float
        Percentual de utilizacao a partir do qual o status e WARNING.
    crit_pct : float
        Percentual de utilizacao a partir do qual o status e CRITICAL.
    """

    def __init__(self, warn_pct: float, crit_pct: float) -> None:
        """
        Inicializa o apresentador com os thresholds de alerta.

        Parametros
        ----------
        warn_pct : float
            Threshold de WARNING em percentual (ex: 75.0).
        crit_pct : float
            Threshold de CRITICAL em percentual (ex: 100.0).
        """
        self.warn_pct = warn_pct
        self.crit_pct = crit_pct

    def determinar_status(self, total_pct: float) -> tuple:
        """
        Determina o status e o exit code com base no percentual total.

        Parametros
        ----------
        total_pct : float
            Percentual de utilizacao total da interface.

        Retorna
        -------
        tuple of (str, int)
            Par (status, exit_code) onde status e "OK", "WARNING" ou "CRITICAL"
            e exit_code e 0, 1 ou 2 respectivamente.
        """
        if total_pct >= self.crit_pct:
            return "CRITICAL", 2
        if total_pct >= self.warn_pct:
            return "WARNING", 1
        return "OK", 0

    def formatar_velocidade(self, bps: float) -> str:
        """
        Formata um valor em bits por segundo para unidade legivel.

        Parametros
        ----------
        bps : float
            Velocidade em bits por segundo.

        Retorna
        -------
        str
            Velocidade formatada (ex: "94.32 Mbps", "1.20 Gbps", "512 Kbps").
        """
        if bps >= 1_000_000_000: return f"{bps / 1_000_000_000:.2f} Gbps"
        if bps >= 1_000_000:     return f"{bps / 1_000_000:.2f} Mbps"
        if bps >= 1_000:         return f"{bps / 1_000:.2f} Kbps"
        return f"{bps:.0f} bps"

    def formatar_label_link(self, bps: int) -> str:
        """
        Retorna o label descritivo da tecnologia de rede com base no link speed.

        Parametros
        ----------
        bps : int
            Velocidade maxima do link em bits por segundo.

        Retorna
        -------
        str
            Descricao da tecnologia (ex: "Gigabit (1 Gbps)", "Fast Ethernet (100 Mbps)").
        """
        if bps >= 100_000_000_000: return "100 Gbps"
        if bps >= 40_000_000_000:  return "40 Gbps"
        if bps >= 25_000_000_000:  return "25 Gbps"
        if bps >= 10_000_000_000:  return "10 Gbps"
        if bps >= 1_000_000_000:   return "Gigabit (1 Gbps)"
        if bps >= 100_000_000:     return "Fast Ethernet (100 Mbps)"
        if bps >= 10_000_000:      return "10 Mbps"
        return "Desconhecida"

    def exibir(self, interface: str, link_bps: int, metricas: dict) -> int:
        """
        Exibe os resultados no stdout e retorna o exit code para o TRMM.

        O formato de saida e:
            Linha 1: STATUS - Utilizacao total: X%
            Linha 2: Interface e tipo de link
            Linha 3: Taxa e percentual de download
            Linha 4: Taxa e percentual de upload
            Linha 5: Perfdata Nagios (iniciada com '|') para graficos do TRMM

        A linha de perfdata e mantida separada e no final para evitar
        truncamento pelo TRMM, que limita o stdout exibido por linha.

        Parametros
        ----------
        interface : str
            Nome amigavel da interface monitorada.
        link_bps : int
            Velocidade maxima do link em bits por segundo.
        metricas : dict
            Dicionario retornado por CalculadorUtilizacao.calcular().

        Retorna
        -------
        int
            Exit code (0=OK, 1=WARNING, 2=CRITICAL).
        """
        status, exit_code = self.determinar_status(metricas["total_pct"])

        w = self.warn_pct
        c = self.crit_pct
        d = metricas["download_pct"]
        u = metricas["upload_pct"]
        t = metricas["total_pct"]

        print(f"{status} d={d}% u={u}% t={t}% | download={d}%;{w};{c};0;100 upload={u}%;{w};{c};0;100 total={t}%;{w};{c};0;100")

        return exit_code


# ==============================================================================
# Orquestrador — coordena todas as camadas
# ==============================================================================

class MonitorRede:
    """
    Orquestra a execucao completa do monitoramento de rede.

    Coordena as camadas de coleta (ColetorNIC), calculo (CalculadorUtilizacao)
    e apresentacao (ApresentadorTRMM), seguindo o fluxo:
        1. Valida interface e obtem link speed
        2. Coleta snapshot 1 dos bytes acumulados (Win32_PerfRawData)
        3. Aguarda intervalo de amostragem (2 segundos)
        4. Coleta snapshot 2 dos bytes acumulados
        5. Calcula taxa real como delta/tempo
        6. Exibe resultado e retorna exit code

    O uso de Win32_PerfRawData com dois snapshots controlados evita o
    problema do Win32_PerfFormattedData, que pode retornar zero quando
    consultado fora do ciclo de atualizacao interno do WMI (1 segundo).

    Parametros
    ----------
    interface : str
        Nome amigavel da interface de rede a monitorar.
    warn_pct : float
        Threshold de WARNING em percentual.
    crit_pct : float
        Threshold de CRITICAL em percentual.
    intervalo : float
        Intervalo em segundos entre os dois snapshots (padrao: 2.0).
        Valores maiores aumentam a precisao em redes com trafego esporadico.
    """

    def __init__(
        self,
        interface: str,
        warn_pct: float,
        crit_pct: float,
        intervalo: float = 2.0
    ) -> None:
        """
        Inicializa o monitor com interface, thresholds e intervalo de amostragem.

        Parametros
        ----------
        interface : str
            Nome amigavel da interface de rede (ex: "Ethernet 3").
        warn_pct : float
            Percentual a partir do qual o status passa a WARNING.
        crit_pct : float
            Percentual a partir do qual o status passa a CRITICAL.
        intervalo : float
            Segundos entre os dois snapshots de bytes acumulados (padrao: 2.0).
        """
        self.interface     = interface
        self.intervalo     = intervalo
        executor           = ExecutorPowerShell()
        self._coletor      = ColetorNIC(interface, executor)
        self._apresentador = ApresentadorTRMM(warn_pct, crit_pct)

    def executar(self) -> int:
        """
        Executa o fluxo completo de monitoramento e retorna o exit code.

        Fluxo:
            1. Obtem link speed — sai com UNKNOWN (3) se nao encontrar.
            2. Coleta snapshot 1 (bytes acumulados via Win32_PerfRawData).
            3. Aguarda self.intervalo segundos.
            4. Coleta snapshot 2.
            5. Calcula taxa = delta_bytes / delta_tempo * 8 bits.
            6. Calcula percentuais e exibe resultado.

        Retorna
        -------
        int
            Exit code final: 0 (OK), 1 (WARNING), 2 (CRITICAL) ou 3 (UNKNOWN).
        """
        # Etapa 1: link speed
        link_bps = self._coletor.obter_link_speed_bps()
        if link_bps == 0:
            interfaces = self._coletor.listar_interfaces()
            print(f"UNKNOWN - Interface '{self.interface}' nao encontrada ou sem link speed.")
            print(f"Interfaces disponiveis: {', '.join(interfaces) if interfaces else 'nenhuma'}")
            return 3

        # Etapa 2: snapshot 1
        snap1 = self._coletor.obter_bytes_acumulados()
        if snap1[0] < 0:
            print(f"UNKNOWN - Nao foi possivel coletar estatisticas para '{self.interface}'.")
            print("Verifique se o nome da interface esta correto e se ha privilegios suficientes.")
            return 3

        # Etapa 3: aguarda intervalo de amostragem
        t1 = time.monotonic()
        time.sleep(self.intervalo)

        # Etapa 4: snapshot 2
        snap2 = self._coletor.obter_bytes_acumulados()
        if snap2[0] < 0:
            print(f"UNKNOWN - Falha na segunda coleta de estatisticas para '{self.interface}'.")
            return 3

        # Etapa 5: calculo
        delta      = time.monotonic() - t1
        calculador = CalculadorUtilizacao(link_bps)
        metricas   = calculador.calcular(snap1, snap2, delta)

        # Etapa 6: exibicao
        return self._apresentador.exibir(self.interface, link_bps, metricas)


# ==============================================================================
# Ponto de entrada
# ==============================================================================

if __name__ == "__main__":
    interface = sys.argv[1] if len(sys.argv) > 1 else "Ethernet"
    monitor   = MonitorRede(interface, warn_pct=75.0, crit_pct=100.0, intervalo=2.0)
    sys.exit(monitor.executar())