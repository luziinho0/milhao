-- Comunidade do Milhão: quem é Ouro debate com outros investidores, com um nome de perfil.
-- Rodar uma vez no SQL Editor do Supabase. Pode rodar de novo sem estragar nada.
-- Quem pode entrar: assinante Ouro em dia, quem está nos 7 dias de teste do Ouro e o criador.

-- 1) Quem pode usar a Comunidade (a mesma regra do site, conferida aqui no banco)
create or replace function public.comunidade_pode(uid uuid) returns boolean
language sql stable security definer set search_path = public, auth as $$
  select uid is not null and (
    uid = '8b812444-cd71-4e90-931a-a45a777a38e0'::uuid
    or exists (select 1 from public.assinaturas a where a.user_id = uid
               and ((a.plano = 'ouro' and a.valido_ate > now()) or a.status = 'criador'))
    or exists (select 1 from auth.users u where u.id = uid
               and greatest(u.created_at, '2026-11-17T03:00:00Z'::timestamptz) + interval '7 days' > now())
  ) $$;
revoke all on function public.comunidade_pode(uuid) from public, anon;
grant execute on function public.comunidade_pode(uuid) to authenticated;

-- 2) Nome de perfil de cada pessoa (único, sem diferença de maiúscula)
create table if not exists public.comunidade_perfis (
  user_id   uuid primary key default auth.uid() references auth.users(id) on delete cascade,
  apelido   text not null check (apelido ~ '^[A-Za-z0-9_.]{3,20}$'),
  foto      text check (foto like 'https://gyxftcpycrnjryfwpslt.supabase.co/storage/v1/object/public/avatares/%' and char_length(foto) <= 300),
  criado_em timestamptz not null default now(),
  bloqueado boolean not null default false
);
create unique index if not exists comunidade_apelido_unico on public.comunidade_perfis (lower(apelido));
alter table public.comunidade_perfis enable row level security;

-- pode escrever: tem acesso e não foi bloqueado pela moderação
create or replace function public.comunidade_ativo(uid uuid) returns boolean
language sql stable security definer set search_path = public as $$
  select public.comunidade_pode(uid) and exists (select 1 from public.comunidade_perfis p where p.user_id = uid and not p.bloqueado) $$;
revoke all on function public.comunidade_ativo(uuid) from public, anon;
grant execute on function public.comunidade_ativo(uuid) to authenticated;

drop policy if exists "perfis: quem pode ve" on public.comunidade_perfis;
create policy "perfis: quem pode ve" on public.comunidade_perfis for select to authenticated using (public.comunidade_pode(auth.uid()));
drop policy if exists "perfis: cria o proprio" on public.comunidade_perfis;
create policy "perfis: cria o proprio" on public.comunidade_perfis for insert to authenticated
  with check (user_id = auth.uid() and public.comunidade_pode(auth.uid()));
drop policy if exists "perfis: troca o proprio" on public.comunidade_perfis;
create policy "perfis: troca o proprio" on public.comunidade_perfis for update to authenticated
  using (user_id = auth.uid()) with check (user_id = auth.uid());
revoke insert, update on public.comunidade_perfis from authenticated, anon;
grant insert (apelido, foto) on public.comunidade_perfis to authenticated;
grant update (apelido, foto) on public.comunidade_perfis to authenticated;

-- 3) Tópicos
create table if not exists public.comunidade_posts (
  id           uuid primary key default gen_random_uuid(),
  user_id      uuid not null default auth.uid() references public.comunidade_perfis(user_id) on delete cascade,
  tema         text not null check (tema in ('acoes', 'fiis', 'renda_fixa', 'cripto', 'estrategia', 'iniciantes', 'geral')),
  ativo        text check (ativo ~ '^[A-Z0-9]{4,8}$'),
  titulo       text not null check (char_length(titulo) between 5 and 120),
  texto        text not null check (char_length(texto) between 1 and 5000),
  criado_em    timestamptz not null default now(),
  editado_em   timestamptz,
  atividade_em timestamptz not null default now(),
  respostas    int not null default 0,
  removido     boolean not null default false
);
create index if not exists comunidade_posts_atividade on public.comunidade_posts (atividade_em desc);
create index if not exists comunidade_posts_ativo on public.comunidade_posts (ativo);
alter table public.comunidade_posts enable row level security;

drop policy if exists "posts: quem pode le" on public.comunidade_posts;
create policy "posts: quem pode le" on public.comunidade_posts for select to authenticated
  using (public.comunidade_pode(auth.uid()) and (not removido or user_id = auth.uid() or auth.uid() = '8b812444-cd71-4e90-931a-a45a777a38e0'::uuid));
drop policy if exists "posts: escreve" on public.comunidade_posts;
create policy "posts: escreve" on public.comunidade_posts for insert to authenticated
  with check (user_id = auth.uid() and public.comunidade_ativo(auth.uid()));
drop policy if exists "posts: edita o proprio" on public.comunidade_posts;
create policy "posts: edita o proprio" on public.comunidade_posts for update to authenticated
  using (user_id = auth.uid() and not removido and public.comunidade_ativo(auth.uid()))
  with check (user_id = auth.uid() and not removido);
drop policy if exists "posts: apaga o proprio" on public.comunidade_posts;
create policy "posts: apaga o proprio" on public.comunidade_posts for delete to authenticated
  using (user_id = auth.uid() or auth.uid() = '8b812444-cd71-4e90-931a-a45a777a38e0'::uuid);
revoke insert, update on public.comunidade_posts from authenticated, anon;
grant insert (tema, ativo, titulo, texto) on public.comunidade_posts to authenticated;
grant update (tema, ativo, titulo, texto) on public.comunidade_posts to authenticated;

-- 4) Respostas
create table if not exists public.comunidade_respostas (
  id         uuid primary key default gen_random_uuid(),
  post_id    uuid not null references public.comunidade_posts(id) on delete cascade,
  user_id    uuid not null default auth.uid() references public.comunidade_perfis(user_id) on delete cascade,
  texto      text not null check (char_length(texto) between 1 and 3000),
  criado_em  timestamptz not null default now(),
  editado_em timestamptz,
  removido   boolean not null default false
);
create index if not exists comunidade_respostas_post on public.comunidade_respostas (post_id, criado_em);
alter table public.comunidade_respostas enable row level security;

drop policy if exists "respostas: quem pode le" on public.comunidade_respostas;
create policy "respostas: quem pode le" on public.comunidade_respostas for select to authenticated
  using (public.comunidade_pode(auth.uid()) and (not removido or user_id = auth.uid() or auth.uid() = '8b812444-cd71-4e90-931a-a45a777a38e0'::uuid));
drop policy if exists "respostas: escreve" on public.comunidade_respostas;
create policy "respostas: escreve" on public.comunidade_respostas for insert to authenticated
  with check (user_id = auth.uid() and public.comunidade_ativo(auth.uid())
              and exists (select 1 from public.comunidade_posts p where p.id = post_id and not p.removido));
drop policy if exists "respostas: edita a propria" on public.comunidade_respostas;
create policy "respostas: edita a propria" on public.comunidade_respostas for update to authenticated
  using (user_id = auth.uid() and not removido and public.comunidade_ativo(auth.uid()))
  with check (user_id = auth.uid() and not removido);
drop policy if exists "respostas: apaga a propria" on public.comunidade_respostas;
create policy "respostas: apaga a propria" on public.comunidade_respostas for delete to authenticated
  using (user_id = auth.uid() or auth.uid() = '8b812444-cd71-4e90-931a-a45a777a38e0'::uuid);
revoke insert, update on public.comunidade_respostas from authenticated, anon;
grant insert (post_id, texto) on public.comunidade_respostas to authenticated;
grant update (texto) on public.comunidade_respostas to authenticated;

-- 5) Denúncias: qualquer pessoa da Comunidade denuncia; só o criador vê
create table if not exists public.comunidade_denuncias (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid default auth.uid() references auth.users(id) on delete set null,
  post_id     uuid references public.comunidade_posts(id) on delete cascade,
  resposta_id uuid references public.comunidade_respostas(id) on delete cascade,
  motivo      text not null check (motivo in ('ofensa', 'golpe', 'propaganda', 'dados_pessoais', 'outro')),
  detalhe     text check (char_length(detalhe) <= 500),
  criado_em   timestamptz not null default now(),
  resolvida   boolean not null default false,
  check (post_id is not null or resposta_id is not null)
);
alter table public.comunidade_denuncias enable row level security;
drop policy if exists "denuncias: envia" on public.comunidade_denuncias;
create policy "denuncias: envia" on public.comunidade_denuncias for insert to authenticated
  with check (user_id = auth.uid() and public.comunidade_pode(auth.uid()));
drop policy if exists "denuncias: criador ve" on public.comunidade_denuncias;
create policy "denuncias: criador ve" on public.comunidade_denuncias for select to authenticated
  using (auth.uid() = '8b812444-cd71-4e90-931a-a45a777a38e0'::uuid);
revoke insert, update, delete on public.comunidade_denuncias from authenticated, anon;
grant insert (post_id, resposta_id, motivo, detalhe) on public.comunidade_denuncias to authenticated;

-- 6) Automático: data de edição, contagem de respostas e limite por dia
create or replace function public.comunidade_editado() returns trigger language plpgsql as $$
begin
  if new.texto is distinct from old.texto then
    new.editado_em := now();
  elsif tg_table_name = 'comunidade_posts' then
    if new.titulo is distinct from old.titulo then new.editado_em := now(); end if;
  end if;
  return new;
end $$;
drop trigger if exists comunidade_posts_editado on public.comunidade_posts;
create trigger comunidade_posts_editado before update on public.comunidade_posts for each row execute function public.comunidade_editado();
drop trigger if exists comunidade_respostas_editado on public.comunidade_respostas;
create trigger comunidade_respostas_editado before update on public.comunidade_respostas for each row execute function public.comunidade_editado();

create or replace function public.comunidade_contar() returns trigger
language plpgsql security definer set search_path = public as $$
declare pid uuid := coalesce(new.post_id, old.post_id);
begin
  update comunidade_posts set
    respostas = (select count(*) from comunidade_respostas r where r.post_id = pid and not r.removido),
    atividade_em = case when tg_op = 'INSERT' then now() else atividade_em end
  where id = pid;
  return null;
end $$;
drop trigger if exists comunidade_respostas_contar on public.comunidade_respostas;
create trigger comunidade_respostas_contar after insert or delete or update of removido on public.comunidade_respostas
  for each row execute function public.comunidade_contar();

create or replace function public.comunidade_limite() returns trigger
language plpgsql security definer set search_path = public as $$
declare n int;
begin
  if tg_table_name = 'comunidade_posts' then
    select count(*) into n from comunidade_posts where user_id = new.user_id and criado_em > now() - interval '1 day';
    if n >= 15 then raise exception 'limite_diario'; end if;
  elsif tg_table_name = 'comunidade_respostas' then
    select count(*) into n from comunidade_respostas where user_id = new.user_id and criado_em > now() - interval '1 day';
    if n >= 100 then raise exception 'limite_diario'; end if;
  else
    select count(*) into n from comunidade_denuncias where user_id = new.user_id and criado_em > now() - interval '1 day';
    if n >= 20 then raise exception 'limite_diario'; end if;
  end if;
  return new;
end $$;
drop trigger if exists comunidade_posts_limite on public.comunidade_posts;
create trigger comunidade_posts_limite before insert on public.comunidade_posts for each row execute function public.comunidade_limite();
drop trigger if exists comunidade_respostas_limite on public.comunidade_respostas;
create trigger comunidade_respostas_limite before insert on public.comunidade_respostas for each row execute function public.comunidade_limite();
drop trigger if exists comunidade_denuncias_limite on public.comunidade_denuncias;
create trigger comunidade_denuncias_limite before insert on public.comunidade_denuncias for each row execute function public.comunidade_limite();

-- 7) Moderação: só o criador remove, restaura, bloqueia e dá denúncia por resolvida
create or replace function public.comunidade_moderar(tipo text, alvo uuid, acao text) returns void
language plpgsql security definer set search_path = public as $$
begin
  if auth.uid() is distinct from '8b812444-cd71-4e90-931a-a45a777a38e0'::uuid then raise exception 'sem_permissao'; end if;
  if tipo = 'post' then update comunidade_posts set removido = (acao = 'remover') where id = alvo;
  elsif tipo = 'resposta' then update comunidade_respostas set removido = (acao = 'remover') where id = alvo;
  elsif tipo = 'perfil' then update comunidade_perfis set bloqueado = (acao = 'bloquear') where user_id = alvo;
  elsif tipo = 'denuncia' then update comunidade_denuncias set resolvida = true where id = alvo;
  else raise exception 'tipo_invalido';
  end if;
end $$;
revoke all on function public.comunidade_moderar(text, uuid, text) from public, anon;
grant execute on function public.comunidade_moderar(text, uuid, text) to authenticated;

select 'ok' as comunidade;
