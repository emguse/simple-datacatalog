"""デモ用サンプルデータを sample_data/ に生成する。

raw_orders.csv → orders_clean.parquet → daily_sales.jsonl というリネージを想定。
"""

from pathlib import Path

import numpy as np
import pandas as pd

OUT = Path(__file__).resolve().parent.parent / "sample_data"


def main() -> None:
    OUT.mkdir(exist_ok=True)
    rng = np.random.default_rng(42)
    n = 200
    raw = pd.DataFrame(
        {
            "order_id": np.arange(1, n + 1),
            "customer_id": rng.integers(1000, 1050, n),
            "customer_email": [f"user{i}@example.com" for i in rng.integers(1000, 1050, n)],
            "order_ts": pd.Timestamp("2026-10-01")
            + pd.to_timedelta(rng.integers(0, 30 * 24 * 60, n), unit="m"),
            "product": rng.choice(["apple", "banana", "cherry", "durian"], n),
            "quantity": rng.integers(-1, 6, n),  # 不正値（-1, 0）を含む
            "unit_price": rng.choice([120.0, 80.0, 300.0, 980.0], n),
        }
    )
    raw.to_csv(OUT / "raw_orders.csv", index=False)

    clean = raw[raw["quantity"] > 0].drop(columns=["customer_email"]).copy()
    clean["order_date"] = clean["order_ts"].dt.date.astype(str)
    clean["amount"] = clean["quantity"] * clean["unit_price"]
    clean.to_parquet(OUT / "orders_clean.parquet", index=False)

    daily = (
        clean.groupby("order_date")
        .agg(order_count=("order_id", "count"), total_amount=("amount", "sum"))
        .reset_index()
    )
    daily.to_json(OUT / "daily_sales.jsonl", orient="records", lines=True, force_ascii=False)


if __name__ == "__main__":
    main()
