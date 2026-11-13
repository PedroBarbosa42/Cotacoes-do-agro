-- =====================================================================
-- db/002_limpar_filmes.sql  |  Remove as tabelas do catálogo de filmes
-- ATENÇÃO: apaga dados de forma definitiva.
-- Rode SOMENTE depois que o CotaAgro estiver no ar (stack redeployada)
-- e depois de exportar as tabelas no DBeaver, se quiser guardar o histórico.
-- =====================================================================

DROP TABLE IF EXISTS comentarios;
DROP TABLE IF EXISTS favoritos;
DROP TABLE IF EXISTS assistir_depois;