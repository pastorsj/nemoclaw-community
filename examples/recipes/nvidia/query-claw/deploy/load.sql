\set ON_ERROR_STOP on

DROP TABLE IF EXISTS delivery_outcomes;
DROP TABLE IF EXISTS shipment_events;
DROP TABLE IF EXISTS purchase_orders;
DROP TABLE IF EXISTS suppliers;
DROP TABLE IF EXISTS facilities;
DROP TABLE IF EXISTS products;

CREATE TABLE suppliers (
  supplier_id text PRIMARY KEY,
  supplier_name text NOT NULL,
  region text NOT NULL,
  criticality text NOT NULL
);
CREATE TABLE facilities (
  facility_id text PRIMARY KEY,
  name text NOT NULL,
  region text NOT NULL,
  lane_risk double precision NOT NULL
);
CREATE TABLE products (
  product_id text PRIMARY KEY,
  name text NOT NULL,
  unit_price_usd numeric NOT NULL
);
CREATE TABLE purchase_orders (
  order_id text PRIMARY KEY,
  supplier_id text NOT NULL REFERENCES suppliers,
  product_id text NOT NULL REFERENCES products,
  facility_id text NOT NULL REFERENCES facilities,
  order_date date NOT NULL,
  promised_date date NOT NULL,
  quantity integer NOT NULL,
  amount_usd numeric NOT NULL,
  status_at_cutoff text NOT NULL,
  split text NOT NULL
);
CREATE TABLE shipment_events (
  event_id text PRIMARY KEY,
  order_id text NOT NULL REFERENCES purchase_orders,
  event_date date NOT NULL,
  event_type text NOT NULL,
  severity text NOT NULL
);
CREATE TABLE delivery_outcomes (
  outcome_id text PRIMARY KEY,
  order_id text NOT NULL REFERENCES purchase_orders,
  outcome_date date NOT NULL,
  outcome text NOT NULL
);

COPY suppliers FROM '/query-claw-data/suppliers.csv' CSV HEADER;
COPY facilities FROM '/query-claw-data/facilities.csv' CSV HEADER;
COPY products FROM '/query-claw-data/products.csv' CSV HEADER;
COPY purchase_orders FROM '/query-claw-data/purchase_orders.csv' CSV HEADER NULL '';
COPY shipment_events FROM '/query-claw-data/shipment_events.csv' CSV HEADER;
COPY delivery_outcomes FROM '/query-claw-data/delivery_outcomes.csv' CSV HEADER;
