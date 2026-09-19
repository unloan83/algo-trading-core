from typing import Tuple

def calculate_indian_transaction_costs(
    buy_price: float,
    sell_price: float,
    qty: int,
    is_intraday: bool = False,
    slippage_pct: float = 0.05
) -> Tuple[float, float, float, float]:
    """
    Calculates exact Indian tax and transaction charges (STT, Brokerage, GST, Stamp Duty, Slippage).
    Returns: (gross_pnl, net_pnl, total_costs, total_slippage_rupees)
    """
    turnover_buy = buy_price * qty
    turnover_sell = sell_price * qty
    total_turnover = turnover_buy + turnover_sell

    # Slippage simulation (0.05% on entry and exit)
    effective_buy = buy_price * (1.0 + slippage_pct / 100.0)
    effective_sell = sell_price * (1.0 - slippage_pct / 100.0)
    
    gross_pnl = (sell_price - buy_price) * qty
    slippage_cost = (effective_buy - buy_price) * qty + (sell_price - effective_sell) * qty

    # Brokerage: Upstox Equity Intraday Plan (Verified live 2026-09-19: lower of ₹20 or 0.05% of turnover per executed order)
    BROKERAGE_FLAT_INR = 20.0
    BROKERAGE_PCT = 0.0005  # 0.05%
    brokerage_buy = min(BROKERAGE_FLAT_INR, BROKERAGE_PCT * turnover_buy)
    brokerage_sell = min(BROKERAGE_FLAT_INR, BROKERAGE_PCT * turnover_sell)
    total_brokerage = brokerage_buy + brokerage_sell

    # STT
    if is_intraday:
        stt = 0.00025 * turnover_sell  # 0.025% on sell side
    else:
        stt = 0.001 * turnover_buy + 0.001 * turnover_sell  # 0.1% on buy and sell

    # Exchange turnover charges (NSE ~0.00345%)
    exchange_charges = 0.0000345 * total_turnover

    # SEBI charges (0.0001%)
    sebi_charges = 0.000001 * total_turnover

    # Stamp duty
    if is_intraday:
        stamp_duty = 0.00003 * turnover_buy # 0.003% on buy
    else:
        stamp_duty = 0.00015 * turnover_buy # 0.015% on buy

    # GST (18% on Brokerage + Exchange charges + SEBI charges)
    gst = 0.18 * (total_brokerage + exchange_charges + sebi_charges)

    total_statutory_costs = total_brokerage + stt + exchange_charges + sebi_charges + stamp_duty + gst
    total_costs = total_statutory_costs + slippage_cost

    net_pnl = gross_pnl - total_costs

    return gross_pnl, net_pnl, total_costs, slippage_cost
