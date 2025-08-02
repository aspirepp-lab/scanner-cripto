import ccxt
import pandas as pd
import time
import datetime
import requests
import os
import logging
import json
from threading import Thread, Lock, Event
from queue import Queue
from flask import Flask, request
from ta.trend import EMAIndicator, MACD, ADXIndicator
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange
from ta.volume import OnBalanceVolumeIndicator

try:
    import pandas_ta as pta
except ImportError:
    raise Exception("Biblioteca pandas_ta não instalada. Execute: pip install pandas_ta")

import numpy as np
npNaN = np.nan

import re
def formatar_string(x):
    return re.sub(r"([a-z])([A-Z])", r"\g<1> \g<2>", x).title()

# ===============================
# === CONFIGURAÇÕES E PARÂMETROS
# ===============================
top_n = 100
timeframe = '4h'
limite_candles = 100
intervalo_em_segundos = 60 * 15
TEMPO_REENVIO = 60 * 60

TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
WEBHOOK_URL = os.getenv("REPLIT_WEBHOOK_URL")
if not TOKEN or not CHAT_ID:
    raise Exception("Defina as variáveis TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID.")
if not WEBHOOK_URL:
    WEBHOOK_URL = "https://scanner-cripto.onrender.com"

ARQUIVO_ALERTAS = 'alertas_enviados.db'
ARQUIVO_CACHE_MOEDAS = 'cache_top_moedas.json'

logging.basicConfig(filename='scanner_criptos.log',
                    level=logging.INFO,
                    format='%(asctime)s [%(levelname)s] %(message)s',
                    datefmt='%Y-%m-%d %H:%M:%S')

import shelve
lock_alertas = Lock()
scanner_ativo = Event()

# ===============================
# === CONTROLE DE COMANDOS TELEGRAM
# ===============================
app = Flask(__name__)

@app.route(f"/{TOKEN}", methods=['POST'])
def receber_mensagem():
    global scanner_ativo
    msg = request.get_json()
    if 'message' in msg and 'text' in msg['message']:
        texto = msg['message']['text']
        chat_id_msg = msg['message']['chat']['id']

        if str(chat_id_msg) != str(CHAT_ID):
            return "Ignorado", 200

        if texto == '/start':
            scanner_ativo.set()
            enviar_telegram("✅ *Scanner ativado!*")
        elif texto == '/stop':
            scanner_ativo.clear()
            enviar_telegram("🛑 *Scanner pausado!*")
        elif texto == '/status':
            status = "✅ Ativo" if scanner_ativo.is_set() else "⛔️ Inativo"
            enviar_telegram(f"📊 *Status atual:* {status}")
    return "OK", 200

def configurar_webhook():
    try:
        endpoint = f"https://api.telegram.org/bot{TOKEN}/setWebhook"
        resposta = requests.post(endpoint, json={"url": f"{WEBHOOK_URL}/{TOKEN}"})
        if resposta.ok:
            print("✅ Webhook configurado com sucesso!")
        else:
            print(f"❌ Erro ao configurar webhook: {resposta.text}")
    except Exception as e:
        print(f"⚠️ Erro ao configurar webhook: {e}")

def iniciar_flask():
    app.run(host="0.0.0.0", port=8080)

# =========================
# === ALERTAS E ARQUIVOS
# =========================
def carregar_alertas():
    try:
        with shelve.open(ARQUIVO_ALERTAS) as db:
            return dict(db)
    except Exception as e:
        logging.error(f"Erro ao carregar alertas: {e}")
        return {}

def salvar_alertas(alertas_dict):
    try:
        with shelve.open(ARQUIVO_ALERTAS) as db:
            for chave, valor in alertas_dict.items():
                db[chave] = valor
    except Exception as e:
        logging.error(f"Erro ao salvar alertas: {e}")

alertas_enviados = carregar_alertas()
pares_alertados_no_ciclo = set()

def pode_enviar_alerta(par, setup):
    agora = datetime.datetime.utcnow()
    chave = f"{par}_{setup}"
    with lock_alertas:
        if chave in alertas_enviados:
            delta = (agora - alertas_enviados[chave]).total_seconds()
            if delta < TEMPO_REENVIO:
                return False
        alertas_enviados[chave] = agora
        salvar_alertas(alertas_enviados)
        return True

# 🧠 Continuação: setups, indicadores, loop principal etc.
# (me avise para colar o restante aqui com segurança e sem truncar)
def detectar_candle_forte(df):
    candle = df.iloc[-1]
    corpo = abs(candle['close'] - candle['open'])
    sombra_sup = candle['high'] - max(candle['close'], candle['open'])
    sombra_inf = min(candle['close'], candle['open']) - candle['low']
    return corpo > sombra_sup and corpo > sombra_inf

def detectar_martelo(df):
    c = df.iloc[-1]
    corpo = abs(c['close'] - c['open'])
    sombra_inf = c['low'] - min(c['close'], c['open'])
    sombra_sup = c['high'] - max(c['close'], c['open'])
    return sombra_inf > corpo * 2 and sombra_sup < corpo

def detectar_engolfo(df):
    c1 = df.iloc[-2]
    c2 = df.iloc[-1]
    return c2['close'] > c2['open'] and c1['close'] < c1['open'] and c2['open'] < c1['close'] and c2['close'] > c1['open']

def calcular_supertrend(df, period=10, multiplier=3):
    df['supertrend'] = pta.supertrend(
        high=df['high'], low=df['low'], close=df['close'],
        length=period, multiplier=multiplier
    )['SUPERT_10_3.0']
    df['supertrend'] = df['supertrend'] > 0
    return df

def obter_dados_fundamentais():
    try:
        total = requests.get("https://api.coingecko.com/api/v3/global").json()
        market_data = total.get('data', {})

        market_cap = market_data.get('total_market_cap', {}).get('usd')
        btc_dominance = market_data.get('market_cap_percentage', {}).get('btc')

        if market_cap is None or btc_dominance is None or market_cap == 0:
            return "*⚠️ Dados de capitalização e dominação BTC indisponíveis no momento.*"

        medo_ganancia = requests.get("https://api.alternative.me/fng/?limit=1").json()
        indice = medo_ganancia['data'][0]
        fear_greed = f"{indice['value']} ({indice['value_classification']})"

        return (
            f"*🌍 MERCADO AGORA*\n"
            f"• Capitalização: ${market_cap:,.0f}\n"
            f"• Domínio BTC: {btc_dominance:.1f}%\n"
            f"• Medo/ganância: {fear_greed}"
        )
    except Exception as e:
        logging.warning(f"Erro ao obter dados fundamentais: {e}")
        return "*⚠️ Dados fundamentais indisponíveis no momento.*"

def obter_eventos_macroeconomicos():
    hoje = datetime.datetime.utcnow().strftime('%Y-%m-%d')
    eventos = [
        {"data": "2025-07-31", "evento": "📊 Divulgação PIB EUA"},
        {"data": "2025-08-01", "evento": "💼 Payroll (Emprego) EUA"},
        {"data": "2025-08-15", "evento": "🏦 Reunião do FOMC (juros Fed)"},
    ]
    eventos_hoje = [e["evento"] for e in eventos if e["data"] == hoje]
    if eventos_hoje:
        return "*📅 Eventos Econômicos Hoje:*\n" + "\n".join(eventos_hoje)
    return ""

# === SETUPS ===
def verificar_setup_1(r, df):
    if all([
        r['rsi'] < 40,
        df['ema9'].iloc[-2] < df['ema21'].iloc[-2] and r['ema9'] > r['ema21'],
        r['macd'] > r['macd_signal'],
        r['adx'] > 20,
        df['volume'].iloc[-1] > df['volume'].mean() * 1.5,
        df['supertrend'].iloc[-1]
    ]):
        return {'setup': '🎯 SETUP 1 – Rigoroso', 'prioridade': '🟠 PRIORIDADE ALTA', 'emoji': '🎯'}
    return None

def verificar_setup_2(r, df):
    if all([
        r['rsi'] < 50,
        r['ema9'] > r['ema21'],
        r['macd'] > r['macd_signal'],
        r['adx'] > 15,
        df['volume'].iloc[-1] > df['volume'].mean()
    ]):
        return {'setup': '⚙️ SETUP 2 – Intermediário', 'prioridade': '🟡 PRIORIDADE MÉDIA-ALTA', 'emoji': '⚙️'}
    return None

def verificar_setup_3(r, df):
    condicoes = [
        r['ema9'] > r['ema21'],
        r['adx'] > 15,
        df['volume'].iloc[-1] > df['volume'].mean()
    ]
    if sum(condicoes) >= 2:
        return {'setup': '🔹 SETUP 3 – Leve', 'prioridade': '🔵 PRIORIDADE MÉDIA', 'emoji': '🔹'}
    return None

def verificar_setup_4(r, df):
    candle_reversao = detectar_martelo(df) or detectar_engolfo(df)
    rsi_subindo = df['rsi'].iloc[-1] > df['rsi'].iloc[-2]
    if (
        r['obv'] > df['obv'].mean() and
        df['close'].iloc[-2] > df['open'].iloc[-2] and
        df['close'].iloc[-1] > df['close'].iloc[-2] and
        candle_reversao and
        rsi_subindo
    ):
        return {'setup': '🔁 SETUP 4 – Reversão Técnica', 'prioridade': '🟣 OPORTUNIDADE DE REVERSÃO', 'emoji': '🔁'}
    return None

def verificar_setup_5(r, df):
    condicoes = [
        r['rsi'] < 40,
        df['ema9'].iloc[-2] < df['ema21'].iloc[-2] and r['ema9'] > r['ema21'],
        r['macd'] > r['macd_signal'],
        r['atr'] > df['atr'].mean(),
        r['obv'] > df['obv'].mean(),
        r['adx'] > 20,
        r['close'] > r['ema200'],
        df['volume'].iloc[-1] > df['volume'].mean(),
        df['supertrend'].iloc[-1],
        detectar_candle_forte(df)
    ]
    if sum(condicoes) >= 6:
        return {'setup': '🔥 SETUP 5 – Alta Confluência', 'prioridade': '🟥 PRIORIDADE MÁXIMA', 'emoji': '🔥'}
    return None

def verificar_setup_6(r, df):
    resistencia = df['high'].iloc[-10:-1].max()
    rompimento = r['close'] > resistencia
    volume_ok = df['volume'].iloc[-1] > df['volume'].mean()
    rsi_ok = r['rsi'] > 55 and df['rsi'].iloc[-1] > df['rsi'].iloc[-2]
    if rompimento and volume_ok and rsi_ok and df['supertrend'].iloc[-1]:
        return {'setup': '🚀 SETUP 6 – Rompimento com Confluência', 'prioridade': '🟩 ALTA OPORTUNIDADE DE CONTINUAÇÃO', 'emoji': '🚀'}
    return None

def enviar_telegram(mensagem):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": mensagem, "parse_mode": "Markdown"}
    headers = {"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"}
    try:
        response = requests.post(url, data=payload, headers=headers, timeout=10)
        if not response.ok:
            logging.error(f"Erro ao enviar Telegram: {response.text}")
        return response.ok
    except Exception as e:
        logging.error(f"Exception ao enviar Telegram: {e}")
        return False

def enviar_alerta_completo(par, r, setup_info):
    if setup_info['setup'] == '🔹 SETUP 3 – Leve' and par in pares_alertados_no_ciclo:
        print(f"⚠️ Ignorado Setup 3 para {par} (já houve sinal mais forte no ciclo).")
        return

    preco = r['close']
    stop = round(preco - r['atr'] * 1.5, 4)
    alvo = round(preco + r['atr'] * 3, 4)

    agora_utc = datetime.datetime.utcnow()
    agora_local = agora_utc - datetime.timedelta(hours=3)
    timestamp_br = agora_local.strftime('%d/%m/%Y %H:%M (Brasília)')
    symbol_okx = par.replace("/", "").upper()
    link_tv = f"https://www.tradingview.com/chart/?symbol=OKX:{symbol_okx}"

    resumo_mercado = obter_dados_fundamentais()
    eventos_dia = obter_eventos_macroeconomicos()

    mensagem = (
        f"{setup_info['emoji']} *{setup_info['setup']}*\n"
        f"{setup_info['prioridade']}\n\n"
        f"📊 Par: `{par}`\n"
        f"💰 Preço: `{preco}`\n"
        f"🛑 Stop: `{stop}` (1.5x ATR)\n"
        f"🎯 Alvo: `{alvo}` (3x ATR)\n"
        f"📈 RSI: {round(r['rsi'], 2)} | ADX: {round(r['adx'], 2)}\n"
        f"📏 ATR: {round(r['atr'], 4)} | Volume: {round(r['volume'], 2)}\n"
        f"🕘 {timestamp_br}\n"
        f"📉 [Abrir gráfico]({link_tv})\n\n"
    )

    if eventos_dia:
        mensagem += eventos_dia + "\n\n"
    mensagem += resumo_mercado

    if pode_enviar_alerta(par, setup_info['setup']):
        if enviar_telegram(mensagem):
            pares_alertados_no_ciclo.add(par)
            logging.info(f"Alerta enviado: {par} - {setup_info['setup']}")
            print(f"✅ Alerta enviado: {par} - {setup_info['setup']}")
        else:
            logging.error(f"Falha ao enviar alerta para {par} - {setup_info['setup']}")
            print(f"❌ Falha ao enviar alerta para {par} - {setup_info['setup']}")
    else:
        print(f"⏳ Alerta já enviado recentemente para {par} - {setup_info['setup']}")

def analisar_moeda(exchange, par, resultados):
    try:
        ohlcv = exchange.fetch_ohlcv(par, timeframe, limit=limite_candles)
        if len(ohlcv) < limite_candles:
            logging.warning(f"Dados insuficientes para {par}")
            return

        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

        close = df['close']
        high = df['high']
        low = df['low']
        volume = df['volume']

        df['ema9'] = EMAIndicator(close, 9).ema_indicator()
        df['ema21'] = EMAIndicator(close, 21).ema_indicator()
        df['ema200'] = EMAIndicator(close, 200).ema_indicator()
        df['rsi'] = RSIIndicator(close, 14).rsi()
        df['atr'] = AverageTrueRange(high, low, close, 14).average_true_range()
        macd = MACD(close)
        df['macd'] = macd.macd()
        df['macd_signal'] = macd.macd_signal()
        df['adx'] = ADXIndicator(high, low, close, 14).adx()
        df['obv'] = OnBalanceVolumeIndicator(close, volume).on_balance_volume()
        df = calcular_supertrend(df)

        r = df.iloc[-1]

        for verificar in [
            verificar_setup_5,
            verificar_setup_6,
            verificar_setup_1,
            verificar_setup_2,
            verificar_setup_4,
            verificar_setup_3
        ]:
            setup_info = verificar(r, df)
            if setup_info:
                enviar_alerta_completo(par, r, setup_info)
                resultados.append(par)
                break

    except Exception as e:
        logging.error(f"Erro na análise de {par}: {e}")
        print(f"❌ Erro com {par}: {e}")

def obter_top_moedas(top_n):
    agora = time.time()

    if os.path.exists(ARQUIVO_CACHE_MOEDAS):
        with open(ARQUIVO_CACHE_MOEDAS, 'r') as f:
            try:
                cache = json.load(f)
                if agora - cache['timestamp'] < 86400:
                    return cache['moedas']
            except Exception as e:
                logging.warning(f"Erro ao carregar cache: {e}")

    url = "https://api.coingecko.com/api/v3/coins/markets"
    params = {
        'vs_currency': 'usd',
        'order': 'market_cap_desc',
        'per_page': top_n,
        'page': 1,
        'sparkline': False
    }

    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        dados = response.json()
        moedas = [coin['symbol'].upper() for coin in dados]

        with open(ARQUIVO_CACHE_MOEDAS, 'w') as f:
            json.dump({'timestamp': agora, 'moedas': moedas}, f)

        return moedas
    except Exception as e:
        logging.error(f"Erro ao obter top moedas: {e}")
        return ['BTC', 'ETH', 'OKB']
