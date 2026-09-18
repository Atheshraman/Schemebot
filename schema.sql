create extension if not exists vector;

create table if not exists schemes (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  ministry text,
  state text,
  description text,
  benefits text,
  application_url text,
  source_url text,
  youtube_url text,
  scraped_at timestamptz default now(),
  embedding vector(384)
);

alter table schemes add column if not exists youtube_url text;

create table if not exists eligibility_criteria (
  id uuid primary key default gen_random_uuid(),
  scheme_id uuid references schemes(id) on delete cascade,
  field text,
  operator text,
  value text
);

-- Drop old ivfflat index if it exists (ivfflat needs minimum rows to work)
drop index if exists schemes_embedding_idx;

-- Use hnsw which works with any number of rows
create index if not exists schemes_embedding_hnsw_idx
  on schemes using hnsw (embedding vector_cosine_ops);

create or replace function match_schemes(
  query_vector vector(384),
  match_count int default 20
)
returns table (
  id uuid,
  name text,
  description text,
  benefits text,
  similarity float
)
language sql
as $$
  select
    id,
    name,
    description,
    benefits,
    1 - (embedding <=> query_vector) as similarity
  from schemes
  order by embedding <=> query_vector
  limit match_count;
$$;
