from market_data import get_bars, compute_indicators, get_vix

print("Fetching SPY data...")
df = get_bars("SPY", "1y", "1d")
print(f"Got {len(df)} bars")
df = compute_indicators(df)
d  = df.iloc[-1]
print(f"Close:    {d['close']:.2f}")
print(f"EMA 20:   {d['ema20']:.2f}")
print(f"EMA 50:   {d['ema50']:.2f}")
print(f"EMA 200:  {df['ema200'].dropna().iloc[-1]:.2f}")
print(f"RSI:      {d['rsi']:.1f}")
print(f"ATR:      {d['atr']:.2f}")
print(f"ADX:      {d['adx']:.1f}")
print(f"MACD:     {d['macd_hist']:.4f}")
print(f"Vol:      {d['vol_ratio']:.2f}x")
print(f"VIX:      {get_vix():.1f}")
print("\nALL INDICATORS OK — no llvmlite/numba needed")
