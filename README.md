# 🎬 Catálogo de Filmes — Microsserviços, Autenticação, Papéis de Admin, Auditoria, Perfil e Plano Premium

## O que é

Uma aplicação web de catálogo de filmes: usuários se cadastram, navegam pela filmografia de Tom Hanks (via API pública do TMDB), favoritam títulos, montam uma lista "Assistir depois" e comentam. Além disso, existe uma camada de controle de acesso por papéis (`usuario` / `admin`), um sistema de auditoria que registra as ações relevantes do sistema, uma página de perfil com foto guardada num object storage próprio e um **plano Premium pago**, cobrado por um provedor de pagamento (Stripe, em modo de teste).

A aplicação é dividida em microsserviços independentes, cada um em seu próprio container Docker — essa decisão está detalhada na seção [Arquitetura](#️-arquitetura).

> Projeto acadêmico desenvolvido para a disciplina do professor [@siriani](https://github.com/siriani).

**Ambiente publicado:** https://pedro-ferreira-isw055.lapps.studio

## **Este projeto continua em:** https://github.com/PedroBarbosa42/Cotacoes-do-agro

## 📚 Documentação reversa

A especificação do sistema, o diagrama de arquitetura, as regras de negócio extraídas do código, o modelo de dados e as dívidas técnicas estão em **[Documentação.md](./Documentação.md)**.

## 📄 Relatório (P1)

Metodologia, arquitetura e evidências de cada entrega do bimestre, em PDF:
📥 [docs/P1_ISW055_Pedro_Ferreira.pdf](docs/P1_ISW055_Pedro_Ferreira.pdf)

---

## 🏗️ Arquitetura

A aplicação é dividida em cinco peças, comunicando-se pela rede interna do Docker:

```
                    Internet
                        │
                        ▼
              ┌───────────────────┐
              │   catalogo_web     │  ← único ponto público (porta 8225)
              │  (Flask, templates)│
              └──┬──────┬──────┬───┘
                 │      │      │  rede interna Docker (sem porta exposta)
                 ▼      ▼      ▼
      ┌──────────┐ ┌──────────┐ ┌──────────┐
      │ auth_api │ │log_service│ │  minio   │
      │(sem UI)  │ │ (sem UI)  │ │(object   │
      └────┬─────┘ └────┬─────┘ │ storage) │
           │            │       └────┬─────┘
           ▼            ▼            ▼
   ┌───────────────┐ ┌────────────┐ ┌──────────────┐
   │ MySQL externo │ │Redis Stream│ │ bucket        │
   │ (usuarios,    │ │"auditoria" │ │ "perfis"      │
   │ favoritos,    │ │(XADD/      │ │ (fotos de     │
   │ comentários,  │ │ XREVRANGE) │ │  perfil)      │
   │ assistir_...) │ └────────────┘ └──────────────┘
   └───────────────┘

   Serviços externos (SaaS): Stripe (pagamento), TMDB (filmes), Brevo (e-mail)
```

- **`catalogo_service` (`catalogo_web`)** — o único serviço com porta publicada para fora (`8225:5000`). Serve todas as páginas (catálogo, login, cadastro, perfil, telas de admin), guarda a sessão do usuário logado, integra com o Stripe e conversa com os outros serviços quando precisa. É ele quem decide, a cada requisição, o que o usuário logado pode ou não fazer.
- **`auth_service` (`auth_api`)** — **não tem porta publicada para o host**, só é alcançável pela rede interna do Docker. Concentra tudo relacionado a identidade: cadastro, login, papéis (`role`), exclusão de usuário e recuperação de senha (envio de e-mail via SMTP).
- **`log_service`** — também sem porta publicada. Recebe eventos de auditoria do `catalogo_web` e grava num Redis Stream. Não sabe nada sobre permissões ou regras de negócio, só registra o que já aconteceu.
- **`minio`** — object storage, também **sem porta publicada**. Guarda os arquivos de foto de perfil. O banco de dados nunca guarda o arquivo em si, só a **chave** de onde ele está (`foto_key`, na tabela `usuarios`). Exibir a foto de volta é o `catalogo_web` buscando esse arquivo no MinIO por dentro da rede interna e entregando os bytes pro navegador pela rota `GET /foto/<id>` — o bucket é privado (sem leitura pública nem URL pré-assinada), então o MinIO em si nunca fica exposto na internet.
- **Banco de dados MySQL** — **externo**, fora do `docker-compose.yml` (endereço configurado em `.env` via `DB_HOST`), compartilhado pelo `catalogo_service` e pelo `auth_service`.
- **Redis** — roda dentro do `docker-compose.yml`, também sem porta publicada, e guarda só o histórico de eventos (não é dado de negócio, é log).
- **Stripe** — serviço externo de pagamento. A página onde o cartão é digitado é hospedada por ele; o sistema nunca vê dados de cartão.

A lógica por trás de isolar cada peça: o `auth_service` concentra as operações de identidade num serviço sem porta pública; o `log_service` separa a gravação dos eventos de auditoria do catálogo, com armazenamento no Redis; o `minio` mantém arquivos binários fora do banco relacional; e o `catalogo_web`, sendo o único serviço com porta publicada no Compose, recebe as requisições externas. A retenção dos logs depende do Redis e do volume configurado; isso não constitui uma garantia de armazenamento imutável.

---

## ☁️ Classificação SaaS / PaaS / IaaS do projeto

O projeto atravessa as três camadas de responsabilidade ao mesmo tempo. Quanto mais embaixo, mais coisa eu administro; quanto mais em cima, mais eu só consumo.

| Peça do sistema | Camada | Por quê |
|---|---|---|
| **VM da faculdade (Ubuntu + Docker)** | **IaaS** | O provedor entrega a máquina virtual, o disco e a rede; o sistema operacional e o Docker são instalados e mantidos por mim. |
| **Containers: `catalogo_web`, `auth_api`, `log_service`** | **PaaS** (na prática, sobre a IaaS) | São o meu código empacotado em imagens; faço o deploy sem reinstalar SO a cada versão (Portainer + Docker Compose). *Observação:* eu ainda administro o Docker na VM, então é um "PaaS caseiro", menos gerenciado que um PaaS de nuvem pública. |
| **Redis e MinIO (containers)** | **PaaS** (na prática, sobre a IaaS) | Serviços de apoio (log e object storage) que eu subo como containers; uso a funcionalidade sem me preocupar com a instalação no SO, mas quem opera e atualiza sou eu. |
| **MySQL externo** | **PaaS** | O banco é fornecido pelo professor já instalado e gerenciado: eu recebo só um usuário e senha, crio as tabelas e uso os dados, sem administrar o servidor, o sistema operacional nem a instalação do MySQL. |
| **Stripe** | **SaaS** | Cobrança e armazenamento de dados de cartão ficam 100% com o Stripe; só consumo via API e página hospedada. |
| **Brevo (SMTP)** | **SaaS** | Envio de e-mail transacional feito por um produto pronto de terceiros. |
| **TMDB (API de filmes)** | **SaaS** | Os dados dos filmes vêm de uma API pública; eu apenas consumo. |
| **GitHub / Codespaces** | **SaaS** | Hospedagem do repositório e ambiente de desenvolvimento na nuvem, sem infraestrutura minha. |
| **Proxy na frente do domínio (provavelmente Cloudflare)** | **SaaS** | Camada de entrada gerida por terceiros, antes do tráfego chegar à VM. |

---

## ✨ Funcionalidades

### Conta de usuário
- **Cadastro** (`/register`) — todo novo usuário nasce com `role = usuario`.
- **Login** (`/login`) — autenticação via `auth_api`; a sessão fica guardada num cookie assinado pelo Flask no `catalogo_web`.
- **Logout** (`/logout`).
- **Recuperação de senha** (`/forgot-password` → `/reset-password`) — gera um token seguro de 32 bytes com expiração de 30 minutos, e envia um e-mail com o link de redefinição via SMTP (ver [Configuração de e-mail](#-configuração-de-e-mail)).
- **Perfil** (`/perfil`) — nome, e-mail, bio, plano atual (com os contadores do plano gratuito) e a lista de filmes favoritados. Toda edição (bio ou foto) é sempre aplicada à conta de quem está logado (`session['user_id']`) — não existe um campo de ID de usuário lido da requisição, então não há como um usuário editar o perfil de outro nem manipulando o formulário.
- **Foto de perfil** — upload de imagem (png, jpg, jpeg, gif ou webp, até 5 MB), validado por extensão e tipo de conteúdo antes de aceitar. O arquivo vai pro MinIO; o banco guarda só a referência.

### Catálogo (qualquer usuário logado)
- Navega pela filmografia de Tom Hanks.
- **Favorita** e desfavorita filmes — a remoção só atinge o próprio favorito, porque a consulta já filtra pelo `usuario_id` da sessão.
- **Assistir depois** (`/assistir-depois`) — lista pessoal de filmes para ver mais tarde; adicionar e remover pelo catálogo, e gerenciar na própria página da lista.
- **Comenta** em filmes — os comentários aparecem pra todo mundo, com o nome de quem escreveu.
- Apaga os **próprios** comentários.

### Plano Premium (cobrança via Stripe, modo de teste)
- No perfil, o usuário gratuito clica em **Assinar Premium** (R$ 9,90/mês) e é levado à página de pagamento hospedada pelo Stripe.
- **Plano gratuito:** até **3 favoritos** e até **3 filmes** em "Assistir depois".
- **Plano Premium:** favoritos e "Assistir depois" **sem limite**, e selo ⭐ no perfil.
- Cancelar a assinatura no Stripe devolve o usuário ao plano gratuito, quando o evento de cancelamento é entregue ao webhook.
- Detalhes do funcionamento na seção [Plano Premium](#-plano-premium-stripe).

### Administração (exclusivo de quem tem `role = admin`)
- **Moderação de comentários** — apaga o comentário de qualquer usuário, não só os próprios.
- **Gestão de papéis** (`/admin/usuarios`) — lista todos os usuários cadastrados e permite promover (`usuario → admin`) ou rebaixar (`admin → usuario`) qualquer um.
- **Excluir usuário** — na mesma tela de gestão de papéis, apaga a conta e seus favoritos, comentários e tokens de recuperação de senha. A implementação não remove os registros correspondentes em `assistir_depois`, então eles podem permanecer no banco. Um admin não pode apagar a própria conta enquanto estiver logado.
- **Dashboard de métricas** (`/admin/metricas`) — total de usuários cadastrados, total de favoritos, quantidade de filmes distintos favoritados, total de comentários, e a quebra de usuários por papel.
- **Logs de auditoria** (`/admin/logs`) — consulta os últimos eventos do sistema, do mais recente pro mais antigo.

---

## 💳 Plano Premium (Stripe)

A cobrança de verdade (em **modo de teste**, sem dinheiro real) é delegada a um provedor especializado: o sistema **nunca vê nem guarda dado de cartão** — a página onde o cartão é digitado é hospedada pelo Stripe, e o banco guarda no máximo `stripe_customer_id` e `stripe_subscription_id`.

### Fluxo

1. O usuário logado clica em **Assinar Premium** (`POST /premium/assinar`).
2. O app cria uma **Checkout Session** (modo assinatura) com o id do usuário em `client_reference_id` e redireciona para o Stripe.
3. O usuário paga no Stripe (cartão de teste `4242 4242 4242 4242`, validade futura, CVC qualquer).
4. A confirmação do pagamento é tratada por dois caminhos:
   - **Webhook** — `POST /webhook/stripe`: o Stripe chama o back-end de forma assíncrona. A assinatura `Stripe-Signature` é **validada** antes de qualquer alteração; sem a assinatura correta, a chamada é recusada (400). Em `checkout.session.completed` o usuário vira `premium = TRUE`; em `customer.subscription.deleted` volta a `FALSE`.
   - **Verificação direta na volta do checkout** — `/premium/sucesso` (e o botão "Já paguei, atualizar status", `/premium/sincronizar`): o próprio sistema consulta a API do Stripe e só ativa o premium se a sessão for do usuário logado, estiver paga e tiver assinatura ativa.

> **Por que dois caminhos?** O webhook é o caminho assíncrono oficial do Stripe e continua implementado, validando a assinatura. A verificação direta (uma chamada que sai do meu servidor para o Stripe) foi mantida como redundância, para o caso de o webhook não ser entregue (atraso ou bloqueio de rede/proxy): o Premium ativa pelo caminho que funcionar primeiro.

### Benefício verificável

A diferença de comportamento é comprovável, do mesmo jeito que o papel `admin` já era:

| | Gratuito | Premium |
|---|---|---|
| Favoritos | até 3 | ilimitado |
| Assistir depois | até 3 | ilimitado |
| Selo no perfil | — | ⭐ Premium |

Ao tentar passar do limite, o usuário gratuito é levado ao perfil com um aviso e o log registra `limite_atingido`.

### Configuração (`.env`)

```
STRIPE_SECRET_KEY=sk_test_...      # SEMPRE chave de teste
STRIPE_PRICE_ID=price_...          # preço recorrente mensal criado no painel (modo de teste)
STRIPE_WEBHOOK_SECRET=whsec_...    # segredo do endpoint de webhook (ou do Stripe CLI)
BASE_URL=https://pedro-ferreira-isw055.lapps.studio
```

Para testar o webhook localmente, com o Stripe CLI:

```bash
stripe listen --api-key sk_test_... --events checkout.session.completed,customer.subscription.deleted --forward-to localhost:8225/webhook/stripe
```

---

## 🔐 Permissões por papel

**`usuario`** (padrão de todo cadastro novo) pode:
- Navegar pelo catálogo de filmes.
- Adicionar e remover favoritos e itens de "Assistir depois" (até 3 de cada, no plano gratuito).
- Comentar em filmes.
- Apagar exclusivamente os próprios comentários.
- Editar exclusivamente o próprio perfil (bio e foto).
- Assinar o plano Premium.

**`admin`** herda tudo isso e, além disso, pode:
- **Moderar comentários:** apagar o de qualquer usuário — `POST /deletar-comentario/<id>`.
- **Gerir papéis:** listar usuários e promover/rebaixar qualquer um — `GET /admin/usuarios` e `POST /admin/usuarios/<id>/role`.
- **Excluir usuário:** apagar qualquer conta, exceto a própria — `POST /admin/usuarios/<id>/deletar`.
- **Ver o dashboard de métricas** — `GET /admin/metricas`.
- **Consultar os logs de auditoria** — `GET /admin/logs`.

### 🛡️ Como o controle de acesso funciona

Todas as checagens abaixo rodam no **backend**, a partir do `role` do usuário autenticado (guardado na sessão assinada do Flask, recebida do `auth_api` no login):

- `POST /deletar-comentario/<id>` — permite se o usuário é o autor do comentário **ou** admin; qualquer outro caso → `403`.
- `GET /admin/usuarios`, `POST /admin/usuarios/<id>/role`, `POST /admin/usuarios/<id>/deletar`, `GET /admin/metricas`, `GET /admin/logs` — só permitem se `role == 'admin'` (decorator `@admin_required`); qualquer outro caso → `403` (ou `401` se nem estiver logado).
- `POST /perfil/bio` e `POST /perfil/foto` — atualizam somente a conta identificada por `session['user_id']`. Já `GET /foto/<id>` recebe o ID do usuário pela própria URL e permite que qualquer usuário autenticado consulte a foto associada àquele ID; essa rota não altera o perfil.
- `POST /favoritar` e `POST /assistir-depois/adicionar` — antes de inserir, conferem se o usuário é premium **consultando o banco** (e não a sessão); se não for e já estiver no limite de 3, a ação é bloqueada.
- `POST /webhook/stripe` — não usa sessão (quem chama é o Stripe); a segurança dessa rota é a **validação da assinatura** do webhook.

A gestão de papéis e a exclusão de usuário são divididas em duas pontas: o `catalogo_web` (único ponto público) faz a checagem de admin, e delega a alteração de fato pra endpoints internos do `auth_api` (`GET /usuarios`, `PUT /usuarios/<id>/role`, `DELETE /usuarios/<id>`) — sem porta exposta pra internet. A exclusão em si apaga primeiro os favoritos, comentários e tokens de reset do usuário (pra não violar as chaves estrangeiras) e só depois o usuário.

A sessão do usuário guarda o `role` recebido no momento do login (um cookie assinado com o `SECRET_KEY` do `catalogo_web`, que não pode ser adulterado pelo cliente) — as decisões de permissão são tomadas localmente a partir daí, sem uma nova chamada de rede ao `auth_api` a cada ação. O status **premium**, por outro lado, é lido do banco a cada checagem, porque a confirmação do pagamento chega depois do login e a sessão ficaria desatualizada.

---

## 📋 Logs e Auditoria

Toda ação relevante do sistema deixa um rastro — quem fez, o quê, e quando.

### Por que um microsserviço próprio, e por que Redis

O `log_service` não sabe nada sobre regras de negócio ou permissões: só recebe eventos e grava. Quem decide o que vale a pena logar é sempre o `catalogo_web`, porque é ele quem tem a sessão do usuário logado.

Log de auditoria tem um padrão de uso diferente de dado de negócio: escreve muito, lê pouco, sem transação complexa. Por isso **não** fica no MySQL — vai pro **Redis Streams** (`XADD` pra gravar, `XREVRANGE` pra consultar os mais recentes primeiro), estrutura pensada pra esse tipo de log ordenado no tempo.

### Eventos registrados

| Ação | Quando é disparado |
|---|---|
| `login` | Depois de autenticar com sucesso |
| `logout` | Antes de limpar a sessão |
| `favoritar` / `desfavoritar` | Ao adicionar/remover um favorito |
| `assistir_depois_adicionar` / `assistir_depois_remover` | Ao adicionar/remover um filme da lista "Assistir depois" |
| `limite_atingido` | Quando um usuário gratuito tenta passar de 3 itens (detalhe: `favoritos` ou `assistir_depois`) |
| `comentar` | Ao publicar um comentário |
| `apagar_comentario` | Ao apagar um comentário — o detalhe diz se foi `(próprio)` ou `(moderação)` |
| `atualizar_bio` / `atualizar_foto_perfil` | Ao editar o próprio perfil |
| `checkout_iniciado` | Quando o usuário vai pagar no Stripe |
| `premium_ativado` | Quando o pagamento é confirmado e o usuário vira Premium |
| `acesso_negado` | Toda vez que uma rota de admin barra um usuário comum com `403` |

Cada evento grava `usuario_id`, `acao`, `detalhe`, `ip` e `timestamp` (exibido já convertido pro horário de Brasília, UTC-3).

### Consultar os logs

`GET /admin/logs` (aceita `?n=` pra mudar quantos eventos trazer, padrão 50), protegida pelo mesmo `@admin_required` das outras telas de admin.

---

## 📧 Configuração de e-mail

A função de envio de e-mail (recuperação de senha) foi escrita de forma **genérica**, usando `smtplib` — pode funcionar com diferentes provedores SMTP, alterando as variáveis de ambiente, sem mudar o código. O Mailtrap foi usado somente durante os testes; depois, o ambiente foi configurado com o Brevo para enviar e-mails reais e permitir que os usuários recebam os links para redefinir a senha:

```
MAIL_SERVER=...
MAIL_PORT=...
MAIL_USERNAME=...
MAIL_PASSWORD=...
MAIL_USE_TLS=True   # ou MAIL_USE_SSL=True, dependendo da porta
MAIL_FROM=...       # precisa ser um remetente verificado, no caso do Brevo
```

---

## 🗂️ Estrutura do projeto

```
CatalogoFilmes/
├── .env.example
├── docker-compose.yml
├── README.md
├── Documentação.md               # documentação reversa (spec, arquitetura, regras, dados)
│
├── auth_service/                 # identidade — sem porta pública
│   ├── Dockerfile
│   ├── requirements.txt
│   └── app.py                    # /register /login /forgot-password /reset-password
│                                  # /usuarios (interno) /usuarios/<id>/role (interno)
│                                  # /usuarios/<id> DELETE (interno)
│
├── log_service/                  # auditoria — sem porta pública
│   ├── Dockerfile
│   ├── requirements.txt
│   └── app.py                    # POST /eventos, GET /eventos (grava/lê no Redis)
│
└── catalogo_service/              # público — porta 8225
    ├── Dockerfile
    ├── requirements.txt           # inclui as libs "minio" e "stripe"
    ├── app.py                     # catálogo, admin, perfil, /foto/<id>, assistir depois,
    │                              # /premium/assinar, /premium/sucesso, /premium/sincronizar,
    │                              # /webhook/stripe
    ├── static/
    │   └── style.css
    └── templates/
        ├── base.html
        ├── auth_base.html
        ├── login.html
        ├── register.html
        ├── forgot_password.html
        ├── reset_password.html
        ├── index.html
        ├── assistir_depois.html    # lista "Assistir depois"
        ├── perfil.html             # perfil + plano + upload de foto + bio
        ├── admin_usuarios.html     # gestão de papéis + exclusão de usuário
        ├── admin_metricas.html     # dashboard de métricas
        └── admin_logs.html         # logs de auditoria
```

---

## 🗃️ Modelo de dados

```
usuarios         (id, nome, email, senha_hash, role, bio, foto_key,
                  premium, stripe_customer_id, stripe_subscription_id, criado_em)
favoritos        (id, usuario_id, tmdb_movie_id, titulo, poster_path, criado_em)
assistir_depois  (id, usuario_id, tmdb_movie_id, titulo, poster_path, criado_em)
comentarios      (id, usuario_id, tmdb_movie_id, texto, criado_em)
reset_tokens     (token, usuario_id, criado_em, expira_em, usado)
```

`role` aceita dois valores: `usuario` (padrão) e `admin`. `bio` é um texto curto (até 280 caracteres). `foto_key` é a chave do objeto no MinIO (ex: `7/a1b2c3d4.jpg`) — nunca o arquivo em si. `premium` é `FALSE` por padrão; `stripe_customer_id` e `stripe_subscription_id` guardam só os identificadores do Stripe — **nunca** número de cartão, CVV ou validade. `favoritos` e `assistir_depois` são únicos por (`usuario_id`, `tmdb_movie_id`).

Os eventos de auditoria **não** ficam nessas tabelas — vivem à parte, no Redis Stream `auditoria`, gerenciado pelo `log_service`. As fotos de perfil também não ficam no banco — vivem no bucket `perfis` do MinIO.

Script das alterações do Premium (executar uma vez no MySQL):

```sql
ALTER TABLE usuarios
  ADD COLUMN premium BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN stripe_customer_id VARCHAR(255) NULL,
  ADD COLUMN stripe_subscription_id VARCHAR(255) NULL;

CREATE TABLE assistir_depois (
  id INT AUTO_INCREMENT PRIMARY KEY,
  usuario_id INT NOT NULL,
  tmdb_movie_id INT NOT NULL,
  titulo VARCHAR(255) NOT NULL,
  poster_path VARCHAR(255) NULL,
  criado_em TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uq_assistir_usuario_filme (usuario_id, tmdb_movie_id)
);
```

---

## 🐳 Configuração do Docker (`docker-compose.yml`)

```yaml
services:
  catalogo_web:
    build: ./catalogo_service
    ports:
      - "8225:5000"
    environment:
      - TMDB_API_KEY=${TMDB_API_KEY}
      - DB_HOST=${DB_HOST}
      - DB_USER=${DB_USER}
      - DB_PASSWORD=${DB_PASSWORD}
      - DB_NAME=${DB_NAME}
      - SECRET_KEY=${SECRET_KEY}
      - MINIO_ROOT_USER=${MINIO_ROOT_USER}
      - MINIO_ROOT_PASSWORD=${MINIO_ROOT_PASSWORD}
      - MINIO_BUCKET=${MINIO_BUCKET}
      - STRIPE_SECRET_KEY=${STRIPE_SECRET_KEY}
      - STRIPE_PRICE_ID=${STRIPE_PRICE_ID}
      - STRIPE_WEBHOOK_SECRET=${STRIPE_WEBHOOK_SECRET}
      - BASE_URL=${BASE_URL}
    depends_on:
      - auth_api
      - log_service
      - minio

  auth_api:
    build: ./auth_service
    environment:
      - PYTHONUNBUFFERED=1
      - DB_HOST=${DB_HOST}
      - DB_USER=${DB_USER}
      - DB_PASSWORD=${DB_PASSWORD}
      - DB_NAME=${DB_NAME}
      - MAIL_SERVER=${MAIL_SERVER}
      - MAIL_PORT=${MAIL_PORT}
      - MAIL_USERNAME=${MAIL_USERNAME}
      - MAIL_PASSWORD=${MAIL_PASSWORD}
      - MAIL_USE_TLS=${MAIL_USE_TLS}
      - MAIL_USE_SSL=${MAIL_USE_SSL}
      - MAIL_FROM=${MAIL_FROM}

  log_service:
    build: ./log_service
    environment:
      - PYTHONUNBUFFERED=1
      - REDIS_HOST=redis
      - REDIS_PORT=6379
    depends_on:
      - redis

  redis:
    image: redis:7-alpine
    volumes:
      - redis_data:/data

  minio:
    image: pgsty/minio:RELEASE.2026-08-04T00-00-00Z
    command: server /data --console-address ":9001"
    environment:
      - MINIO_ROOT_USER=${MINIO_ROOT_USER}
      - MINIO_ROOT_PASSWORD=${MINIO_ROOT_PASSWORD}
    volumes:
      - minio_data:/data

volumes:
  redis_data:
  minio_data:
```

O MySQL roda **fora** desse arquivo — o endereço vem de `DB_HOST` no `.env`. O Redis e o MinIO rodam **dentro**, mas nenhum dos dois publica porta pro host — só o `log_service` e o `catalogo_web`, respectivamente, falam com eles pela rede interna.

---

## ▶️ Como rodar localmente

1. Copie `.env.example` para `.env` e preencha com suas credenciais reais (TMDB, banco de dados, `SECRET_KEY`, SMTP — ver [Configuração de e-mail](#-configuração-de-e-mail) —, `MINIO_ROOT_USER`/`MINIO_ROOT_PASSWORD`/`MINIO_BUCKET` e as variáveis do Stripe, **sempre com chaves de teste**).
2. Rode o script SQL do Premium (seção [Modelo de dados](#️-modelo-de-dados)) no seu MySQL.
3. `docker compose up --build -d`
4. Acesse `http://localhost:8225`.
5. Cadastre-se pela tela e promova seu próprio usuário a admin direto no banco:
   ```sql
   UPDATE usuarios SET role = 'admin' WHERE email = 'seu_email_de_teste@x.com';
   ```
   A partir daí, promover os próximos usuários (e apagar contas, se precisar) já pode ser feito pela própria tela `/admin/usuarios`.
6. Para testar o Premium: faça login com um usuário comum, passe do limite de 3 favoritos, assine pelo perfil com o cartão de teste `4242 4242 4242 4242` e confira `premium = 1` no banco.
