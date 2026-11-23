-- Reclame aqui do Milhão: cada pessoa envia e vê as próprias mensagens; só o criador vê todas e responde.
create table if not exists public.reclamacoes (
  id uuid primary key default gen_random_uuid(),
  protocolo text not null unique default upper(substr(md5(random()::text || clock_timestamp()::text), 1, 6)),
  criado_em timestamptz not null default now(),
  user_id uuid default auth.uid() references auth.users(id) on delete set null,
  tipo text not null check (tipo in ('bug', 'dado', 'pagamento', 'sugestao', 'outro')),
  pagina text check (char_length(pagina) <= 60),
  ativo text check (char_length(ativo) <= 12),
  mensagem text not null check (char_length(mensagem) between 5 and 4000),
  contato text check (char_length(contato) <= 160),
  navegador text check (char_length(navegador) <= 300),
  print text check (char_length(print) <= 200),
  status text not null default 'aberta' check (status in ('aberta', 'vendo', 'resolvida')),
  resposta text check (char_length(resposta) <= 4000),
  respondido_em timestamptz
);
alter table public.reclamacoes enable row level security;

drop policy if exists "reclamacoes: envia a propria" on public.reclamacoes;
create policy "reclamacoes: envia a propria" on public.reclamacoes for insert to authenticated
  with check (user_id = auth.uid() and status = 'aberta' and resposta is null and respondido_em is null);
drop policy if exists "reclamacoes: ve as proprias" on public.reclamacoes;
create policy "reclamacoes: ve as proprias" on public.reclamacoes for select to authenticated
  using (user_id = auth.uid() or auth.uid() = '8b812444-cd71-4e90-931a-a45a777a38e0'::uuid);
drop policy if exists "reclamacoes: criador responde" on public.reclamacoes;
create policy "reclamacoes: criador responde" on public.reclamacoes for update to authenticated
  using (auth.uid() = '8b812444-cd71-4e90-931a-a45a777a38e0'::uuid)
  with check (auth.uid() = '8b812444-cd71-4e90-931a-a45a777a38e0'::uuid);

-- no máximo 10 mensagens por pessoa a cada 24 horas
create or replace function public.limite_reclamacoes() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  if (select count(*) from public.reclamacoes where user_id = new.user_id and criado_em > now() - interval '1 day') >= 10 then
    raise exception 'limite_diario';
  end if;
  return new;
end $$;
drop trigger if exists reclamacoes_limite on public.reclamacoes;
create trigger reclamacoes_limite before insert on public.reclamacoes for each row execute function public.limite_reclamacoes();

-- prints: pasta privada, cada pessoa só grava na própria pasta; o criador vê todos
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('reclamacoes', 'reclamacoes', false, 5242880, array['image/jpeg'])
on conflict (id) do nothing;
drop policy if exists "reclamacoes: envia print" on storage.objects;
create policy "reclamacoes: envia print" on storage.objects for insert to authenticated
  with check (bucket_id = 'reclamacoes' and (storage.foldername(name))[1] = auth.uid()::text);
drop policy if exists "reclamacoes: ve print" on storage.objects;
create policy "reclamacoes: ve print" on storage.objects for select to authenticated
  using (bucket_id = 'reclamacoes' and ((storage.foldername(name))[1] = auth.uid()::text
         or auth.uid() = '8b812444-cd71-4e90-931a-a45a777a38e0'::uuid));
select 'ok' as reclame_aqui;
