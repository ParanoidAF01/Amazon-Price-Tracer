-- Run this in your Supabase SQL editor
-- Go to: https://app.supabase.com → your project → SQL Editor → New query

-- Products table
create table if not exists products (
  id            uuid primary key default gen_random_uuid(),
  name          text,
  url           text not null,
  asin          text,
  baseline_price numeric(10,2),
  active        boolean default true,
  created_at    timestamptz default now()
);

-- Price history table
create table if not exists price_history (
  id         bigserial primary key,
  product_id uuid references products(id) on delete cascade,
  price      numeric(10,2) not null,
  checked_at timestamptz default now()
);

-- Index for fast lookups
create index if not exists idx_price_history_product on price_history(product_id, checked_at desc);

-- Enable Row Level Security but allow all (single-user app)
alter table products      enable row level security;
alter table price_history enable row level security;

create policy "allow all" on products      for all using (true) with check (true);
create policy "allow all" on price_history for all using (true) with check (true);
