
-- =====================================================================
-- db/001_dominio.sql  |  CotaAgro: domínio, papéis do negócio e seed
-- Pode ser executado mais de uma vez sem duplicar dados.
-- ANTES de rodar: confira o tipo de usuarios.id com
--     SHOW CREATE TABLE usuarios;
-- Este script assume id INT (com sinal). Se for BIGINT ou UNSIGNED,
-- troque o tipo das colunas usuario_id abaixo para o mesmo tipo.
-- =====================================================================

SET NAMES utf8mb4;

-- 1) Papéis do negócio: produtor (padrão), operador e admin
ALTER TABLE usuarios MODIFY role VARCHAR(20) NOT NULL DEFAULT 'produtor';
UPDATE usuarios SET role = 'produtor' WHERE role = 'usuario';

-- 2) Produtos
CREATE TABLE IF NOT EXISTS produtos (
    id INT AUTO_INCREMENT PRIMARY KEY,
    nome VARCHAR(80) NOT NULL,
    categoria VARCHAR(20) NOT NULL DEFAULT 'grao',
    unidade_padrao VARCHAR(20) NOT NULL DEFAULT 'saca 60kg',
    criado_em TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_produtos_nome (nome)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 3) Praças
CREATE TABLE IF NOT EXISTS pracas (
    id INT AUTO_INCREMENT PRIMARY KEY,
    nome VARCHAR(120) NOT NULL,
    cidade VARCHAR(80) NULL,
    usuario_id INT NOT NULL,
    criado_em TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_pracas_nome (nome),
    CONSTRAINT fk_pracas_usuario FOREIGN KEY (usuario_id)
        REFERENCES usuarios (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 4) Cotações
CREATE TABLE IF NOT EXISTS cotacoes (
    id INT AUTO_INCREMENT PRIMARY KEY,
    produto_id INT NOT NULL,
    praca_id INT NOT NULL,
    usuario_id INT NOT NULL,
    preco DECIMAL(10,2) NOT NULL,
    unidade VARCHAR(20) NOT NULL DEFAULT 'saca 60kg',
    observacao VARCHAR(280) NULL,
    anexo_key VARCHAR(255) NULL,
    atualizado_em TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,
    KEY idx_cotacoes_produto_praca (produto_id, praca_id),
    KEY idx_cotacoes_usuario (usuario_id),
    CONSTRAINT chk_cotacoes_preco CHECK (preco > 0),
    CONSTRAINT fk_cotacoes_produto FOREIGN KEY (produto_id)
        REFERENCES produtos (id) ON DELETE CASCADE,
    CONSTRAINT fk_cotacoes_praca FOREIGN KEY (praca_id)
        REFERENCES pracas (id) ON DELETE CASCADE,
    CONSTRAINT fk_cotacoes_usuario FOREIGN KEY (usuario_id)
        REFERENCES usuarios (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 5) Produtos acompanhados por cada usuário (Free: até 3)
CREATE TABLE IF NOT EXISTS acompanhados (
    usuario_id INT NOT NULL,
    produto_id INT NOT NULL,
    criado_em TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (usuario_id, produto_id),
    CONSTRAINT fk_acomp_usuario FOREIGN KEY (usuario_id)
        REFERENCES usuarios (id) ON DELETE CASCADE,
    CONSTRAINT fk_acomp_produto FOREIGN KEY (produto_id)
        REFERENCES produtos (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- SEED: dados de exemplo (não são preços reais)
INSERT IGNORE INTO produtos (nome, categoria, unidade_padrao) VALUES
    ('Soja', 'grao', 'saca 60kg'),
    ('Milho', 'grao', 'saca 60kg'),
    ('Trigo', 'grao', 'saca 60kg'),
    ('Ureia', 'insumo', 'tonelada'),
    ('Adubo NPK', 'insumo', 'tonelada');

INSERT IGNORE INTO pracas (nome, cidade, usuario_id)
SELECT v.nome, v.cidade, a.id
FROM (
    SELECT 'Cooperativa Agrícola Modelo' AS nome, 'Cidade Exemplo A' AS cidade
    UNION ALL SELECT 'Cerealista Boa Safra', 'Cidade Exemplo B'
    UNION ALL SELECT 'Cooperativa Vale Verde', 'Cidade Exemplo C'
) v
CROSS JOIN (
    SELECT id FROM usuarios
    WHERE role = 'admin'
    ORDER BY id LIMIT 1
) a;

INSERT INTO cotacoes (produto_id, praca_id, usuario_id, preco, unidade, observacao)
SELECT p.id, pr.id, pr.usuario_id, v.preco, p.unidade_padrao, v.obs
FROM (
    SELECT 'Soja' AS produto, 'Cooperativa Agrícola Modelo' AS praca, 131.50 AS preco, 'À vista (exemplo)' AS obs
    UNION ALL SELECT 'Soja', 'Cerealista Boa Safra', 129.90, 'À vista (exemplo)'
    UNION ALL SELECT 'Soja', 'Cooperativa Vale Verde', 132.20, 'Retirada na unidade (exemplo)'
    UNION ALL SELECT 'Milho', 'Cooperativa Agrícola Modelo', 58.40, 'À vista (exemplo)'
    UNION ALL SELECT 'Milho', 'Cerealista Boa Safra', 57.90, 'À vista (exemplo)'
    UNION ALL SELECT 'Milho', 'Cooperativa Vale Verde', 59.10, 'Retirada na unidade (exemplo)'
    UNION ALL SELECT 'Trigo', 'Cooperativa Vale Verde', 86.30, 'À vista (exemplo)'
    UNION ALL SELECT 'Ureia', 'Cooperativa Agrícola Modelo', 2890.00, 'Preço de exemplo'
    UNION ALL SELECT 'Adubo NPK', 'Cerealista Boa Safra', 3340.00, 'Preço de exemplo'
) v
JOIN produtos p ON p.nome = v.produto
JOIN pracas pr ON pr.nome = v.praca
WHERE NOT EXISTS (SELECT 1 FROM cotacoes);
