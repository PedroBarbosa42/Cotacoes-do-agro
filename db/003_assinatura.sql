-- =====================================================================
-- db/003_assinatura.sql  |  CotaAgro: planos e assinatura (Stripe)
-- Pode ser executado mais de uma vez (MariaDB: ADD COLUMN IF NOT EXISTS).
-- =====================================================================

SET NAMES utf8mb4;

-- 1) Cópia local do estado da assinatura (a verdade está no Stripe;
--    quem mantém esta cópia em dia são os webhooks).
ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS premium BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS stripe_customer_id VARCHAR(64) NULL;
ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS stripe_subscription_id VARCHAR(64) NULL;
ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS plano VARCHAR(20) NOT NULL DEFAULT 'free';
ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS status_assinatura VARCHAR(30) NULL;
ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS assinatura_valor_centavos INT NULL;

-- Quem já estava como premium (Plano Premium anterior) passa a constar como Pro.
UPDATE usuarios SET plano = 'pro' WHERE premium = TRUE AND plano = 'free';

-- 2) Eventos do Stripe já processados: o mesmo evento nunca é aplicado duas vezes.
CREATE TABLE IF NOT EXISTS stripe_eventos (
    event_id    VARCHAR(100) NOT NULL PRIMARY KEY,
    tipo        VARCHAR(80)  NOT NULL,
    recebido_em TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;