"""
trmm_chart.py - Gerador de grafico de utilizacao de rede a partir da API do Tactical RMM

Prioridade de configuracao (maior para menor):
    1. Flags CLI      (--url, --key, --file, --output)
    2. Arquivo .env   (TRMM_URL, TRMM_API_KEY, TRMM_FILE, TRMM_OUTPUT)
    3. Variaveis de ambiente do sistema

Arquivo .env (coloque na mesma pasta do script):
    TRMM_URL=https://rmm.empresa.com/api/checks/123/history/
    TRMM_API_KEY=sua-api-key-aqui
    TRMM_OUTPUT=grafico.html
    # TRMM_FILE=exportado.json   # alternativa ao TRMM_URL

Uso via CLI (sobrepoe o .env):
    python trmm_chart.py --url URL --key API_KEY
    python trmm_chart.py --url URL --key API_KEY --output rede.html
    python trmm_chart.py --file dados.json

Argumentos:
    --url    URL da API do TRMM (endpoint de historico do check)
    --key    API Key do TRMM (enviada no header X-API-KEY)
    --file   Alternativa: arquivo JSON local exportado do TRMM
    --output Caminho do HTML gerado (padrao: grafico.html)
    --env    Caminho do arquivo .env (padrao: .env na pasta do script)
"""

import argparse
import json
import os
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path


# ==============================================================================
# Leitor de .env — carrega variaveis de ambiente do arquivo .env
# ==============================================================================

class LeitorEnv:
    """
    Responsavel por carregar variaveis de um arquivo .env e popula-las
    no os.environ, sem sobrescrever variaveis ja definidas no sistema.

    Suporta comentarios (#), linhas em branco e valores com aspas simples
    ou duplas. Nao requer dependencias externas (python-dotenv).

    Variaveis suportadas:
        TRMM_URL       — URL da API do TRMM
        TRMM_API_KEY   — Chave de API (header X-API-KEY)
        TRMM_FILE      — Arquivo JSON local (alternativa ao TRMM_URL)
        TRMM_OUTPUT    — Caminho do HTML gerado (padrao: grafico.html)
    """

    def carregar(self, caminho: str | None = None) -> None:
        """
        Le o arquivo .env e popula os.environ com as variaveis encontradas.

        Variaveis ja presentes no ambiente do sistema nao sao sobrescritas,
        garantindo que variaveis de CI/CD ou shell tenham prioridade sobre o .env.

        Parametros
        ----------
        caminho : str or None
            Caminho do arquivo .env. Se None, procura por '.env' na mesma
            pasta do script atual.
        """
        if caminho is None:
            caminho = Path(__file__).parent / ".env"
        else:
            caminho = Path(caminho)

        if not caminho.exists():
            return

        with open(caminho, encoding="utf-8") as f:
            for linha in f:
                linha = linha.strip()
                if not linha or linha.startswith("#"):
                    continue
                if "=" not in linha:
                    continue
                chave, _, valor = linha.partition("=")
                chave = chave.strip()
                valor = valor.strip().strip("'\"")
                # Nao sobrescreve variaveis ja definidas no ambiente
                if chave and chave not in os.environ:
                    os.environ[chave] = valor


# ==============================================================================
# Parser de argumentos CLI
# ==============================================================================

class CliParser:
    """
    Responsavel por definir e parsear os argumentos de linha de comando.

    Aceita duas fontes de dados mutuamente exclusivas:
        - API do TRMM via --url + --key
        - Arquivo JSON local via --file
    """

    def parsear(self) -> argparse.Namespace:
        """
        Define os argumentos aceitos, carrega o .env e retorna o namespace
        com valores finais mesclados (CLI > .env > padroes).

        Ordem de prioridade para cada valor:
            1. Flag CLI explicitamente passada pelo usuario
            2. Variavel de ambiente / .env (TRMM_URL, TRMM_API_KEY, etc.)
            3. Valor padrao do argparse

        Retorna
        -------
        argparse.Namespace
            Objeto com os atributos: url, key, file, output.

        Raises
        ------
        SystemExit
            Se nenhuma fonte de dados (url ou file) for encontrada,
            ou se --url for usado sem --key e sem TRMM_API_KEY no ambiente.
        """
        ap = argparse.ArgumentParser(
            description="Gera grafico HTML de utilizacao de rede a partir do Tactical RMM.",
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog=__doc__,
        )

        ap.add_argument(
            "--env",
            metavar="ARQUIVO_ENV",
            default=None,
            help="Caminho do arquivo .env (padrao: .env na pasta do script)",
        )
        ap.add_argument(
            "--url",
            metavar="URL",
            default=None,
            help="URL da API do TRMM — sobrepoe TRMM_URL do .env",
        )
        ap.add_argument(
            "--file",
            metavar="ARQUIVO",
            default=None,
            help="Arquivo JSON local exportado do TRMM — sobrepoe TRMM_FILE do .env",
        )
        ap.add_argument(
            "--key",
            metavar="API_KEY",
            default=None,
            help="API Key do TRMM (X-API-KEY) — sobrepoe TRMM_API_KEY do .env",
        )
        ap.add_argument(
            "--output",
            metavar="SAIDA",
            default=None,
            help="Caminho do HTML gerado — sobrepoe TRMM_OUTPUT do .env (padrao: grafico.html)",
        )

        args = ap.parse_args()

        # Carrega .env antes de aplicar fallbacks
        LeitorEnv().carregar(args.env)

        # Aplica fallbacks do ambiente (.env ou sistema) onde CLI nao foi passado
        if not args.url:
            args.url = os.environ.get("TRMM_URL")
        if not args.file:
            args.file = os.environ.get("TRMM_FILE")
        if not args.key:
            args.key = os.environ.get("TRMM_API_KEY")
        if not args.output:
            args.output = os.environ.get("TRMM_OUTPUT", "grafico.html")

        # Valida: precisa de url ou file
        if not args.url and not args.file:
            ap.error(
                "Informe --url ou --file, ou defina TRMM_URL / TRMM_FILE no .env"
            )

        # url e file sao mutuamente exclusivos
        if args.url and args.file:
            ap.error("--url e --file sao mutuamente exclusivos.")

        # key obrigatoria com url
        if args.url and not args.key:
            ap.error(
                "--key e obrigatorio com --url. "
                "Defina TRMM_API_KEY no .env ou passe --key."
            )

        return args


# ==============================================================================
# Buscador de dados — API ou arquivo local
# ==============================================================================

class BuscadorDados:
    """
    Responsavel por carregar os dados brutos do TRMM, seja via
    requisicao HTTP autenticada ou leitura de arquivo JSON local.
    """

    def da_api(self, url: str, api_key: str) -> list:
        """
        Busca o historico de um check via API REST do TRMM.

        Usa metodo PATCH com body vazio, conforme exigido pela API do Tactical RMM.
        Envia o header X-API-KEY para autenticacao. Utiliza urllib nativo.

        Parametros
        ----------
        url : str
            URL completa do endpoint de historico do check.
        api_key : str
            Chave de API do TRMM.

        Retorna
        -------
        list
            Lista de registros JSON retornados pela API.

        Raises
        ------
        SystemExit
            Se a requisicao falhar (timeout, HTTP error, JSON invalido).
        """
        print(f"Buscando dados: {url}")
        req = urllib.request.Request(
            url,
            data=b"{}",
            method="PATCH",
            headers={
                "X-API-KEY": api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                corpo = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            print(f"Erro HTTP {e.code}: {e.reason}")
            print("Verifique a URL e a API Key.")
            sys.exit(1)
        except urllib.error.URLError as e:
            print(f"Erro de conexao: {e.reason}")
            sys.exit(1)

        try:
            dados = json.loads(corpo)
        except json.JSONDecodeError as e:
            print(f"Resposta nao e JSON valido: {e}")
            sys.exit(1)

        return dados if isinstance(dados, list) else list(dados.values())[0]

    def do_arquivo(self, caminho: str) -> list:
        """
        Carrega os dados de um arquivo JSON local exportado do TRMM.

        Parametros
        ----------
        caminho : str
            Caminho para o arquivo JSON de entrada.

        Retorna
        -------
        list
            Lista de registros JSON lidos do arquivo.

        Raises
        ------
        SystemExit
            Se o arquivo nao existir ou o conteudo for JSON invalido.
        """
        print(f"Lendo arquivo: {caminho}")
        try:
            with open(caminho, encoding="utf-8") as f:
                dados = json.load(f)
        except FileNotFoundError:
            print(f"Arquivo nao encontrado: {caminho}")
            sys.exit(1)
        except json.JSONDecodeError as e:
            print(f"JSON invalido: {e}")
            sys.exit(1)

        return dados if isinstance(dados, list) else list(dados.values())[0]


# ==============================================================================
# Parser de stdout — extrai metricas do campo stdout do TRMM
# ==============================================================================

class ParserStdout:
    """
    Responsavel por extrair os valores de download, upload e total
    do campo stdout gerado pelo script net_utilization.py.

    Formato esperado: "STATUS d=X% u=Y% t=Z% | perfdata..."
    """

    _RE_DOWNLOAD = re.compile(r"d=([\d\.]+)%")
    _RE_UPLOAD   = re.compile(r"u=([\d\.]+)%")
    _RE_TOTAL    = re.compile(r"t=([\d\.]+)%")

    def parsear(self, stdout: str) -> dict | None:
        """
        Extrai download, upload e total de uma linha de stdout.

        Parametros
        ----------
        stdout : str
            Linha de saida do script net_utilization.py.

        Retorna
        -------
        dict com chaves 'download', 'upload', 'total' (floats em %)
        ou None se o formato nao for reconhecido.
        """
        md = self._RE_DOWNLOAD.search(stdout)
        mu = self._RE_UPLOAD.search(stdout)
        mt = self._RE_TOTAL.search(stdout)

        if not (md and mu and mt):
            return None

        return {
            "download": float(md.group(1)),
            "upload":   float(mu.group(1)),
            "total":    float(mt.group(1)),
        }


# ==============================================================================
# Processador de registros — converte raw JSON em registros estruturados
# ==============================================================================

class ProcessadorRegistros:
    """
    Responsavel por converter os registros brutos da API do TRMM
    em uma lista estruturada de medicoes, filtrando entradas invalidas.

    Parametros
    ----------
    parser : ParserStdout
        Instancia do parser de stdout.
    """

    def __init__(self, parser: ParserStdout) -> None:
        """
        Inicializa o processador com o parser de stdout.

        Parametros
        ----------
        parser : ParserStdout
            Instancia responsavel por extrair as metricas do stdout.
        """
        self._parser = parser

    def processar(self, dados: list) -> list:
        """
        Processa a lista bruta de registros e retorna medicoes validas.

        Cada registro valido e um dict com:
            timestamp : datetime  — horario UTC convertido para timezone local
            download  : float     — percentual de download
            upload    : float     — percentual de upload
            total     : float     — percentual total

        Registros com retcode fora de {0,1,2} ou sem metricas parseadas
        sao descartados silenciosamente.

        Parametros
        ----------
        dados : list
            Lista bruta de registros retornados pela API ou arquivo.

        Retorna
        -------
        list of dict
            Registros validos ordenados cronologicamente.
        """
        registros = []
        for entrada in dados:
            try:
                x       = entrada["x"]
                stdout  = entrada["results"]["stdout"]
                retcode = entrada["results"].get("retcode", 0)
            except (KeyError, TypeError):
                continue

            if retcode not in (0, 1, 2):
                continue

            metricas = self._parser.parsear(stdout)
            if metricas is None:
                continue

            ts = datetime.fromisoformat(x.replace("Z", "+00:00")).astimezone()
            registros.append({"timestamp": ts, **metricas})

        registros.sort(key=lambda r: r["timestamp"])
        return registros


# ==============================================================================
# Gerador de grafico — produz HTML interativo com Chart.js e zoom
# ==============================================================================

class GeradorGrafico:
    """
    Responsavel por gerar um arquivo HTML com grafico interativo
    de utilizacao de rede usando Chart.js com plugin de zoom/pan.

    O grafico exibe:
        - Linha de Download (pontos + area preenchida)
        - Linha de Upload (pontos + area preenchida)
        - Linha de Total (tracejada, sem pontos)
        - Linha de threshold WARN em 75%

    Suporta zoom via scroll do mouse e pan via arraste.

    Parametros
    ----------
    caminho_saida : str
        Caminho do arquivo HTML a ser gerado.
    """

    def __init__(self, caminho_saida: str) -> None:
        """
        Inicializa o gerador com o caminho de saida.

        Parametros
        ----------
        caminho_saida : str
            Caminho completo do arquivo HTML de saida.
        """
        self.caminho_saida = caminho_saida

    def _formatar_timestamp(self, ts: datetime) -> str:
        """
        Formata um datetime para exibicao legivel no eixo X.

        Parametros
        ----------
        ts : datetime
            Timestamp do registro.

        Retorna
        -------
        str
            String no formato "DD/MM HH:MM:SS".
        """
        return ts.strftime("%d/%m %H:%M:%S")

    def gerar(self, registros: list) -> None:
        """
        Gera o arquivo HTML com o grafico a partir dos registros.

        Parametros
        ----------
        registros : list of dict
            Lista de registros retornados por ProcessadorRegistros.processar().

        Raises
        ------
        SystemExit
            Se nao houver registros validos para plotar.
        """
        if not registros:
            print("Nenhum registro valido encontrado para gerar o grafico.")
            sys.exit(1)

        labels    = [self._formatar_timestamp(r["timestamp"]) for r in registros]
        downloads = [r["download"] for r in registros]
        uploads   = [r["upload"]   for r in registros]
        totais    = [r["total"]    for r in registros]

        labels_js    = json.dumps(labels)
        downloads_js = json.dumps(downloads)
        uploads_js   = json.dumps(uploads)
        totais_js    = json.dumps(totais)

        n      = len(registros)
        inicio = self._formatar_timestamp(registros[0]["timestamp"])
        fim    = self._formatar_timestamp(registros[-1]["timestamp"])
        gerado = datetime.now().strftime("%d/%m/%Y %H:%M:%S")

        html = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Utilizacao de Rede — TRMM</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/hammerjs@2.0.8/hammer.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-zoom@2.0.1/dist/chartjs-plugin-zoom.min.js"></script>
<style>
  :root {{
    --bg:      #0d1117;
    --surf:    #161b22;
    --border:  #30363d;
    --text:    #e6edf3;
    --muted:   #8b949e;
    --blue:    #58a6ff;
    --green:   #3fb950;
    --orange:  #f78166;
    --warn:    rgba(210,153,34,0.55);
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: var(--bg);
    color: var(--text);
    font-family: 'Courier New', monospace;
    min-height: 100vh;
    padding: 2rem 1.5rem;
  }}
  header {{
    display: flex;
    justify-content: space-between;
    align-items: flex-end;
    flex-wrap: wrap;
    gap: 0.5rem;
    border-bottom: 1px solid var(--border);
    padding-bottom: 1rem;
    margin-bottom: 1.5rem;
  }}
  header h1 {{ font-size: 1rem; letter-spacing: 0.14em; text-transform: uppercase; color: var(--blue); }}
  header span {{ font-size: 0.72rem; color: var(--muted); }}
  .card {{
    background: var(--surf);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 1.5rem;
    max-width: 1100px;
    margin: 0 auto;
  }}
  .hint {{ font-size: 0.7rem; color: var(--muted); margin-bottom: 0.75rem; }}
  .chart-wrap {{ position: relative; height: 420px; }}
  .stats {{
    display: flex;
    gap: 1rem;
    margin-top: 1.25rem;
    flex-wrap: wrap;
  }}
  .stat {{
    flex: 1;
    min-width: 130px;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 0.7rem 1rem;
  }}
  .stat .l {{ font-size: 0.62rem; letter-spacing: 0.1em; text-transform: uppercase; color: var(--muted); margin-bottom: 0.25rem; }}
  .stat .v {{ font-size: 1.35rem; font-weight: bold; }}
  .stat .s {{ font-size: 0.68rem; color: var(--muted); margin-top: 0.15rem; }}
  .dl {{ color: var(--blue); }}
  .ul {{ color: var(--green); }}
  .tot {{ color: var(--orange); }}
  footer {{ font-size: 0.68rem; color: var(--muted); text-align: center; margin-top: 1.25rem; letter-spacing: 0.06em; }}
  .btn {{
    background: transparent;
    border: 1px solid var(--border);
    color: var(--muted);
    padding: 5px 14px;
    border-radius: 4px;
    cursor: pointer;
    font-family: 'Courier New', monospace;
    font-size: 0.72rem;
    letter-spacing: 0.06em;
    margin-bottom: 0.75rem;
  }}
  .btn:hover {{ border-color: var(--blue); color: var(--blue); }}
</style>
</head>
<body>

<header>
  <h1>&#9632; Utilizacao de Rede — Tactical RMM</h1>
  <span>{inicio} &rarr; {fim} &nbsp;|&nbsp; {n} amostras</span>
</header>

<div class="card">
  <div class="hint">scroll = zoom &nbsp;&middot;&nbsp; arrastar = pan &nbsp;&middot;&nbsp; duplo clique = resetar</div>
  <button class="btn" onclick="chart.resetZoom()">&#8635; resetar zoom</button>
  <div class="chart-wrap">
    <canvas id="chart"></canvas>
  </div>
  <div class="stats" id="stats"></div>
</div>

<footer>gerado em {gerado}</footer>

<script>
const labels    = {labels_js};
const downloads = {downloads_js};
const uploads   = {uploads_js};
const totais    = {totais_js};

function avg(a) {{ return (a.reduce((s,v)=>s+v,0)/a.length).toFixed(2); }}
function max(a) {{ return Math.max(...a).toFixed(2); }}

const warnPlugin = {{
  id: 'warnLine',
  afterDraw(c) {{
    const {{ctx, chartArea, scales}} = c;
    const y = scales.y.getPixelForValue(75);
    ctx.save();
    ctx.strokeStyle = 'rgba(210,153,34,0.5)';
    ctx.lineWidth = 1;
    ctx.setLineDash([5,5]);
    ctx.beginPath();
    ctx.moveTo(chartArea.left, y);
    ctx.lineTo(chartArea.right, y);
    ctx.stroke();
    ctx.fillStyle = 'rgba(210,153,34,0.75)';
    ctx.font = '10px monospace';
    ctx.fillText('warn 75%', chartArea.right - 60, y - 4);
    ctx.restore();
  }}
}};

const chart = new Chart(document.getElementById('chart'), {{
  type: 'line',
  data: {{
    labels,
    datasets: [
      {{
        label: 'Download',
        data: downloads,
        borderColor: '#58a6ff',
        backgroundColor: 'rgba(88,166,255,0.08)',
        pointBackgroundColor: '#58a6ff',
        pointRadius: 4,
        pointHoverRadius: 7,
        borderWidth: 2,
        tension: 0.3,
        fill: true,
      }},
      {{
        label: 'Upload',
        data: uploads,
        borderColor: '#3fb950',
        backgroundColor: 'rgba(63,185,80,0.07)',
        pointBackgroundColor: '#3fb950',
        pointRadius: 4,
        pointHoverRadius: 7,
        borderWidth: 2,
        tension: 0.3,
        fill: true,
      }},
      {{
        label: 'Total',
        data: totais,
        borderColor: '#f78166',
        backgroundColor: 'transparent',
        pointRadius: 0,
        borderWidth: 2,
        borderDash: [6, 4],
        tension: 0.3,
        fill: false,
      }},
    ]
  }},
  options: {{
    responsive: true,
    maintainAspectRatio: false,
    interaction: {{ mode: 'index', intersect: false }},
    plugins: {{
      legend: {{
        labels: {{
          color: '#8b949e',
          font: {{ family: 'Courier New', size: 11 }},
          boxWidth: 14,
          padding: 20,
        }}
      }},
      tooltip: {{
        backgroundColor: '#161b22',
        borderColor: '#30363d',
        borderWidth: 1,
        titleColor: '#e6edf3',
        bodyColor: '#8b949e',
        titleFont: {{ family: 'Courier New', size: 11 }},
        bodyFont:  {{ family: 'Courier New', size: 11 }},
        callbacks: {{
          label: c => ` ${{c.dataset.label}}: ${{c.parsed.y.toFixed(2)}}%`
        }}
      }},
      zoom: {{
        pan: {{ enabled: true, mode: 'x' }},
        zoom: {{
          wheel: {{ enabled: true }},
          pinch: {{ enabled: true }},
          mode: 'x',
        }},
        limits: {{ x: {{ min: 'original', max: 'original' }} }}
      }}
    }},
    scales: {{
      x: {{
        ticks: {{
          color: '#8b949e',
          font: {{ family: 'Courier New', size: 10 }},
          maxRotation: 45,
          maxTicksLimit: 12,
        }},
        grid: {{ color: 'rgba(48,54,61,0.6)' }}
      }},
      y: {{
        min: 0,
        max: 100,
        ticks: {{
          callback: v => v + '%',
          stepSize: 10,
          color: '#8b949e',
          font: {{ family: 'Courier New', size: 10 }},
        }},
        grid: {{ color: 'rgba(48,54,61,0.6)' }}
      }}
    }}
  }},
  plugins: [warnPlugin]
}});

document.getElementById('stats').innerHTML = `
  <div class="stat"><div class="l">Download max</div><div class="v dl">${{max(downloads)}}%</div><div class="s">media ${{avg(downloads)}}%</div></div>
  <div class="stat"><div class="l">Upload max</div><div class="v ul">${{max(uploads)}}%</div><div class="s">media ${{avg(uploads)}}%</div></div>
  <div class="stat"><div class="l">Total max</div><div class="v tot">${{max(totais)}}%</div><div class="s">media ${{avg(totais)}}%</div></div>
  <div class="stat"><div class="l">Amostras</div><div class="v" style="color:#8b949e">{n}</div><div class="s">{inicio} &rarr; {fim}</div></div>
`;
</script>
</body>
</html>"""

        with open(self.caminho_saida, "w", encoding="utf-8") as f:
            f.write(html)

        print(f"Grafico gerado : {self.caminho_saida}")
        print(f"Registros      : {n}")
        print(f"Periodo        : {inicio} -> {fim}")


# ==============================================================================
# Orquestrador
# ==============================================================================

class Orquestrador:
    """
    Coordena o fluxo completo: parse de argumentos, busca de dados,
    processamento e geracao do grafico HTML.
    """

    def executar(self) -> None:
        """
        Executa o pipeline completo de geracao do grafico.

        Fluxo:
            1. Parseia argumentos CLI (CliParser)
            2. Busca dados via API ou arquivo local (BuscadorDados)
            3. Processa e filtra registros (ProcessadorRegistros)
            4. Gera o HTML com grafico interativo (GeradorGrafico)
        """
        args        = CliParser().parsear()
        buscador    = BuscadorDados()
        processador = ProcessadorRegistros(ParserStdout())
        gerador     = GeradorGrafico(args.output)

        if args.url:
            dados = buscador.da_api(args.url, args.key)
        else:
            dados = buscador.do_arquivo(args.file)

        registros = processador.processar(dados)
        gerador.gerar(registros)


# ==============================================================================
# Ponto de entrada
# ==============================================================================

if __name__ == "__main__":
    Orquestrador().executar()