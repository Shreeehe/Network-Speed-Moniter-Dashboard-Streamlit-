-- Run this once in Supabase: Dashboard -> SQL Editor -> New query -> paste -> Run

-- =========================================================================
-- 1. NEW OFFICE TABLE: network_log
-- =========================================================================
create table if not exists public.network_log (
  id                            bigint generated always as identity primary key,
  ts                            timestamptz not null,
  computer_status               text not null,
  ping_status                   text,
  ping_avg_ms                   numeric,
  ping_loss_percent             numeric,
  download_mbps                 numeric,
  upload_mbps                   numeric,
  idle_latency_ms               numeric,
  speedtest_packet_loss_percent numeric,
  server                        text,
  network_status                text,
  note                          text,
  result_url                    text,
  -- lets the monitor re-send rows safely without creating duplicates
  unique (ts, computer_status)
);

create index if not exists network_log_ts_idx on public.network_log (ts desc);

-- Security: RLS on. The monitor writes with secret/service_role key (bypasses RLS).
-- Streamlit reads with anon key (read-only).
alter table public.network_log enable row level security;

drop policy if exists "anon can read" on public.network_log;
create policy "anon can read"
  on public.network_log for select
  to anon
  using (true);

-- Optional: If you prefer using the anon key for inserting from local monitor
drop policy if exists "anon can insert" on public.network_log;
create policy "anon can insert"
  on public.network_log for insert
  to anon
  with check (true);


-- =========================================================================
-- 2. OLD OFFICE TABLE: network_log_old_office
-- =========================================================================
create table if not exists public.network_log_old_office (
  id                            bigint generated always as identity primary key,
  ts                            timestamptz not null,
  computer_status               text not null,
  ping_status                   text,
  ping_avg_ms                   numeric,
  ping_loss_percent             numeric,
  download_mbps                 numeric,
  upload_mbps                   numeric,
  idle_latency_ms               numeric,
  speedtest_packet_loss_percent numeric,
  server                        text,
  network_status                text,
  note                          text,
  result_url                    text,
  -- lets the monitor re-send rows safely without creating duplicates
  unique (ts, computer_status)
);

create index if not exists network_log_old_office_ts_idx on public.network_log_old_office (ts desc);

alter table public.network_log_old_office enable row level security;

drop policy if exists "anon can read old office" on public.network_log_old_office;
create policy "anon can read old office"
  on public.network_log_old_office for select
  to anon
  using (true);

drop policy if exists "anon can insert old office" on public.network_log_old_office;
create policy "anon can insert old office"
  on public.network_log_old_office for insert
  to anon
  with check (true);

