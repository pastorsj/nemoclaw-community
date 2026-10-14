-- SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
-- SPDX-License-Identifier: Apache-2.0
--
-- Physical schema of the synthetic_market DuckDB: the tables the market importer writes (demo_data/market.py).
-- It is the real-market schema plus company_news, the ticker-linked news table the news tools read.
-- demo-data runs this, then loads each table with INSERT INTO main.<table> BY NAME SELECT * FROM
-- read_parquet('tables/<table>.parquet'). Primary and foreign keys here are also the keys of the Auto Ontology model.
CREATE TABLE main.assets (
  asset_id VARCHAR PRIMARY KEY, company_name VARCHAR NOT NULL, cik VARCHAR, sic_code VARCHAR NOT NULL,
  sector VARCHAR NOT NULL, industry VARCHAR NOT NULL, exchange VARCHAR NOT NULL, profile VARCHAR,
  first_session DATE NOT NULL, last_session DATE NOT NULL, sessions INTEGER NOT NULL,
  median_dollar_volume DOUBLE, liquidity_rank INTEGER NOT NULL UNIQUE, is_synthetic BOOLEAN NOT NULL
);
CREATE TABLE main.ticker_history (
  ticker_history_id VARCHAR PRIMARY KEY,
  asset_id VARCHAR NOT NULL REFERENCES main.assets(asset_id), ticker VARCHAR NOT NULL,
  effective_from DATE NOT NULL, effective_to DATE
);
CREATE TABLE main.trading_sessions (
  session_id VARCHAR PRIMARY KEY, trading_date DATE NOT NULL UNIQUE, close_at TIMESTAMPTZ NOT NULL,
  symbols INTEGER NOT NULL
);
CREATE TABLE main.daily_prices (
  price_id VARCHAR PRIMARY KEY, asset_id VARCHAR NOT NULL REFERENCES main.assets(asset_id),
  trading_date DATE NOT NULL, open DOUBLE NOT NULL, high DOUBLE NOT NULL, low DOUBLE NOT NULL,
  close DOUBLE NOT NULL, adjusted_close DOUBLE NOT NULL, volume BIGINT NOT NULL, dollar_volume DOUBLE NOT NULL,
  bar_count INTEGER NOT NULL, total_return_1d DOUBLE NOT NULL, UNIQUE(asset_id, trading_date)
);
CREATE TABLE main.asset_relationships (
  relationship_id VARCHAR PRIMARY KEY,
  source_asset_id VARCHAR NOT NULL REFERENCES main.assets(asset_id),
  target_asset_id VARCHAR NOT NULL REFERENCES main.assets(asset_id), relationship_type VARCHAR NOT NULL,
  peer_rank INTEGER NOT NULL
);
CREATE TABLE main.company_news (
  news_id VARCHAR PRIMARY KEY, primary_asset_id VARCHAR NOT NULL REFERENCES main.assets(asset_id),
  published_at TIMESTAMPTZ NOT NULL, source_name VARCHAR NOT NULL, headline VARCHAR NOT NULL, summary VARCHAR,
  event_type VARCHAR NOT NULL, sentiment_label VARCHAR NOT NULL, is_story BOOLEAN NOT NULL
);
