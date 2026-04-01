import asyncio
import json
import logging
import os
import urllib.request
from datetime import datetime

import websockets

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Configuração ──────────────────────────────────────────────────────────────
DERIV_TOKEN    = os.environ["DERIV_TOKEN"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT  = os.environ["TELEGRAM_CHAT"]

DERIV_WS_URL   = "wss://ws.derivws.com/websockets/v3?app_id=1089"
SYMBOL         = "1HZ100V"
BARRIER_OFFSET = 0.1
MAX_DAILY_LOSS = float(os.environ.get("MAX_DAILY_LOSS", "5000"))

state = {
    "balance":        0.0,
    "daily_loss":     0.0,
    "trades_hoje":    0,
}

_req_id = 0


def next_req_id() -> int:
    global _req_id
    _req_id += 1
    return _req_id


# ── Telegram ──────────────────────────────────────────────────────────────────
async def telegram(msg: str) -> None:
    url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = json.dumps({"chat_id": TELEGRAM_CHAT, "text": msg}).encode()
    req  = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        log.warning(f"Telegram erro: {e}")


# ── Receptor WS ───────────────────────────────────────────────────────────────
async def receptor(ws, fila_ticks: asyncio.Queue, pendentes: dict):
    async for raw in ws:
        data   = json.loads(raw)
        req_id = data.get("req_id")
        if req_id and req_id in pendentes:
            fut = pendentes.pop(req_id)
            if not fut.done():
                fut.set_result(data)
        elif "tick" in data:
            await fila_ticks.put(data["tick"])


async def ws_request(ws, payload: dict, pendentes: dict, timeout: int = 15) -> dict:
    rid               = next_req_id()
    payload["req_id"] = rid
    loop              = asyncio.get_event_loop()
    fut               = loop.create_future()
    pendentes[rid]    = fut
    await ws.send(json.dumps(payload))
    return await asyncio.wait_for(fut, timeout=timeout)


# ── Awesome Oscillator ────────────────────────────────────────────────────────
def calcular_ao(candles: list) -> list:
    """Retorna lista de valores do AO para cada candle a partir do índice 33."""
    medianas = [(c["high"] + c["low"]) / 2 for c in candles]

    def sma(values, period):
        return sum(values) / period

    ao_values = []
    for i in range(33, len(medianas)):
        sma5  = sma(medianas[i-4:i+1], 5)
        sma34 = sma(medianas[i-33:i+1], 34)
        ao_values.append(round(sma5 - sma34, 6))

    return ao_values


def analisar_sinal(ao_values: list) -> str | None:
    """
    Retorna 'HIGHER', 'LOWER' ou None.
    - AO acima do zero + barra vermelha (AO caindo) → HIGHER
    - AO abaixo do zero + barra verde (AO subindo)  → LOWER
    """
    if len(ao_values) < 2:
        return None

    ao_atual   = ao_values[-1]
    ao_anterior = ao_values[-2]
    barra_verde    = ao_atual > ao_anterior
    barra_vermelha = ao_atual < ao_anterior

    if ao_atual > 0 and barra_vermelha:
        return "CALL"
    if ao_atual < 0 and barra_verde:
        return "PUT"
    return None


# ── Busca candles ─────────────────────────────────────────────────────────────
async def buscar_candles(ws, pendentes: dict) -> list | None:
    """Busca os últimos 60 candles de 1 minuto."""
    try:
        resp = await ws_request(ws, {
            "ticks_history": SYMBOL,
            "style":         "candles",
            "granularity":   60,
            "count":         60,
            "end":           "latest",
        }, pendentes, timeout=15)

        candles_raw = resp.get("candles", [])
        if len(candles_raw) < 35:
            log.warning(f"Candles insuficientes: {len(candles_raw)}")
            return None

        return [
            {
                "open":  float(c["open"]),
                "high":  float(c["high"]),
                "low":   float(c["low"]),
                "close": float(c["close"]),
                "epoch": c["epoch"],
            }
            for c in candles_raw
        ]
    except asyncio.TimeoutError:
        log.warning("Timeout ao buscar candles.")
        return None


# ── Segundos até fechar o candle atual ───────────────────────────────────────
def segundos_ate_fechar() -> int:
    """Retorna quantos segundos faltam para o próximo fechamento de minuto."""
    agora = datetime.utcnow()
    return 60 - agora.second


# ── Loop principal ────────────────────────────────────────────────────────────
async def conectar() -> None:
    tentativa = 0
    while True:
        try:
            tentativa += 1
            log.info(f"Conectando... (tentativa {tentativa})")
            async with websockets.connect(DERIV_WS_URL, ping_interval=30, ping_timeout=10) as ws:
                tentativa = 0

                # Autenticação
                await ws.send(json.dumps({"authorize": DERIV_TOKEN, "req_id": 0}))
                auth = json.loads(await ws.recv())
                if "error" in auth:
                    log.error(f"Erro de autenticação: {auth['error']['message']}")
                    return

                state["balance"] = float(auth["authorize"]["balance"])
                log.info(f"Autenticado! Saldo: ${state['balance']:.2f}")
                await telegram(f"Bot AO conectado — saldo: ${state['balance']:.2f}")

                # Subscrição de ticks (para manter conexão viva)
                await ws.send(json.dumps({"ticks": SYMBOL, "subscribe": 1}))

                fila_ticks = asyncio.Queue()
                pendentes  = {}
                task_receptor = asyncio.create_task(receptor(ws, fila_ticks, pendentes))

                try:
                    while True:
                        # Verifica limite diário
                        if state["daily_loss"] >= MAX_DAILY_LOSS:
                            msg = f"⛔ Limite diário atingido. Loss: ${state['daily_loss']:.2f}. Encerrando."
                            log.info(msg)
                            await telegram(msg)
                            task_receptor.cancel()
                            return

                        # Calcula quanto falta para fechar o candle
                        falta = segundos_ate_fechar()
                        log.info(f"Faltam {falta}s para fechar o candle.")

                        # Aguarda até faltar 2s
                        if falta > 2:
                            await asyncio.sleep(falta - 2)

                        # Busca candles e calcula AO
                        candles = await buscar_candles(ws, pendentes)
                        if candles is None:
                            await asyncio.sleep(1)
                            continue

                        ao_values = calcular_ao(candles)
                        sinal     = analisar_sinal(ao_values)

                        ao_atual    = ao_values[-1] if ao_values else 0
                        ao_anterior = ao_values[-2] if len(ao_values) >= 2 else 0
                        cor_barra   = "verde" if ao_atual > ao_anterior else "vermelha"
                        log.info(f"AO: {ao_atual:.4f} | Barra: {cor_barra} | Sinal: {sinal or 'nenhum'}")

                        if sinal is None:
                            # Aguarda o candle fechar e vai para o próximo ciclo
                            await asyncio.sleep(3)
                            continue

                        # Stake = 1% do saldo
                        stake        = round(state["balance"] * 0.01, 2)
                        preco_atual  = candles[-1]["close"]
                        barrier      = round(preco_atual + BARRIER_OFFSET, 2) if sinal == "CALL" else round(preco_atual - BARRIER_OFFSET, 2)
                        barrier_str  = f"+{BARRIER_OFFSET}" if sinal == "CALL" else f"-{BARRIER_OFFSET}"

                        log.info(f"Sinal: {sinal} | Stake: ${stake} | Barrier: {barrier_str} | Preço: {preco_atual}")

                        # Envia ordem
                        try:
                            resp_buy = await ws_request(ws, {
                                "buy": 1,
                                "price": stake,
                                "parameters": {
                                    "contract_type": sinal,
                                    "symbol":        SYMBOL,
                                    "duration":      1,
                                    "duration_unit": "m",
                                    "basis":         "stake",
                                    "amount":        stake,
                                    "currency":      "USD",
                                    "barrier":       barrier_str,
                                },
                            }, pendentes, timeout=10)
                        except asyncio.TimeoutError:
                            log.error("Timeout ao enviar ordem.")
                            await telegram(f"⚠️ Timeout ao enviar ordem {sinal}.")
                            await asyncio.sleep(3)
                            continue

                        if "error" in resp_buy:
                            erro = resp_buy["error"]["message"]
                            log.error(f"Erro na ordem: {erro}")
                            await telegram(f"⚠️ Erro na ordem ({sinal}): {erro}")
                            await asyncio.sleep(3)
                            continue

                        contract_id  = resp_buy["buy"]["contract_id"]
                        preco_compra = resp_buy["buy"]["buy_price"]
                        log.info(f"Ordem aberta — {sinal} — contrato {contract_id} — ${preco_compra:.2f}")

                        # Aguarda 1 minuto + 3s de margem para o contrato encerrar
                        saldo_antes = state["balance"]
                        await asyncio.sleep(63)

                        # Consulta saldo após encerramento
                        try:
                            resp_bal = await ws_request(ws, {"balance": 1}, pendentes, timeout=10)
                            saldo_depois = float(resp_bal.get("balance", {}).get("balance", saldo_antes))
                            state["balance"] = saldo_depois
                        except asyncio.TimeoutError:
                            log.warning("Timeout ao consultar saldo.")
                            saldo_depois = saldo_antes

                        variacao = round(saldo_depois - saldo_antes, 2)
                        if variacao >= 0:
                            emoji = "✅ GANHOU"
                        else:
                            emoji = "❌ PERDEU"
                            state["daily_loss"] += abs(variacao)

                        state["trades_hoje"] += 1

                        msg = (
                            f"{emoji} | {sinal} | barrier {barrier_str}\n"
                            f"Saldo: ${saldo_depois:.2f} (era ${saldo_antes:.2f})\n"
                            f"Variação: ${variacao:+.2f}\n"
                            f"AO: {ao_atual:.4f} | Barra: {cor_barra}\n"
                            f"Trades hoje: {state['trades_hoje']}"
                        )
                        log.info(msg)
                        await telegram(msg)

                        # Pequena pausa antes do próximo ciclo
                        await asyncio.sleep(2)

                finally:
                    task_receptor.cancel()

        except Exception as e:
            wait = min(5 * tentativa, 60)
            log.error(f"Conexão perdida: {e}. Reconectando em {wait}s...")
            await asyncio.sleep(wait)


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    asyncio.run(conectar())
