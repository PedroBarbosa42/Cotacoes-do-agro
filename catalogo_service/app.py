import os
import json
import uuid
import requests
import mysql.connector
import threading
import stripe
from mysql.connector import pooling
from functools import wraps
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, Response, abort
from werkzeug.security import generate_password_hash, check_password_hash
from minio import Minio
from minio.error import S3Error

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'default_secret')
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024

BRASILIA_TZ = timezone(timedelta(hours=-3))

stripe.api_key = os.getenv('STRIPE_SECRET_KEY')
STRIPE_PRICE_ID = os.getenv('STRIPE_PRICE_ID')
STRIPE_WEBHOOK_SECRET = os.getenv('STRIPE_WEBHOOK_SECRET')
BASE_URL = os.getenv('BASE_URL', 'http://localhost:8225').rstrip('/')

LIMITE_GRATIS = 3
ROLES_VALIDOS = ('produtor', 'operador', 'admin')
TABELAS_COM_LIMITE = {'acompanhados'}

db_pool = pooling.MySQLConnectionPool(
    pool_name="catalogo_pool",
    pool_size=5,
    host=os.getenv('DB_HOST'),
    user=os.getenv('DB_USER'),
    password=os.getenv('DB_PASSWORD'),
    database=os.getenv('DB_NAME')
)

def get_db_connection():
    conn = db_pool.get_connection()
    conn.ping(reconnect=True, attempts=3, delay=0.5)
    return conn

MINIO_BUCKET = os.getenv('MINIO_BUCKET', 'perfis')
EXTENSOES_PERMITIDAS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}

minio_client = Minio(
    'minio:9000',
    access_key=os.getenv('MINIO_ROOT_USER'),
    secret_key=os.getenv('MINIO_ROOT_PASSWORD'),
    secure=False
)

def garantir_bucket():
    if not minio_client.bucket_exists(MINIO_BUCKET):
        minio_client.make_bucket(MINIO_BUCKET)
    try:
        minio_client.delete_bucket_policy(MINIO_BUCKET)
    except S3Error:
        pass

try:
    garantir_bucket()
except Exception as e:
    print(f"Aviso: não foi possível preparar o bucket do MinIO no startup: {e}")

def extensao_valida(nome_arquivo):
    return '.' in nome_arquivo and nome_arquivo.rsplit('.', 1)[1].lower() in EXTENSOES_PERMITIDAS

def registrar_evento(usuario_id, acao, detalhe=''):
    ip = request.remote_addr

    def _enviar():
        try:
            requests.post('http://log_service:5002/eventos', json={
                'usuario_id': usuario_id,
                'acao': acao,
                'detalhe': detalhe,
                'ip': ip
            }, timeout=2)
        except requests.exceptions.RequestException:
            pass

    threading.Thread(target=_enviar, daemon=True).start()

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({"erro": "Não autenticado"}), 401
        if session.get('user_role') != 'admin':
            registrar_evento(session['user_id'], 'acesso_negado', f'tentou acessar {request.path}')
            return jsonify({"erro": "Ação restrita a administradores."}), 403
        return f(*args, **kwargs)
    return decorated

def role_required(*roles):
    """Exige login e um dos papéis informados; senão responde 403 e registra no log."""
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if 'user_id' not in session:
                return jsonify({"erro": "Não autenticado"}), 401
            if session.get('user_role') not in roles:
                registrar_evento(session['user_id'], 'acesso_negado', f'tentou acessar {request.path}')
                return jsonify({"erro": "Ação não permitida para o seu papel."}), 403
            return f(*args, **kwargs)
        return decorated
    return decorator

def usuario_eh_premium(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT premium FROM usuarios WHERE id = %s", (user_id,))
    linha = cursor.fetchone()
    cursor.close()
    conn.close()
    return bool(linha and linha[0])

def contar_itens(tabela, user_id):
    if tabela not in TABELAS_COM_LIMITE:
        raise ValueError('tabela inválida')
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(f"SELECT COUNT(*) FROM {tabela} WHERE usuario_id = %s", (user_id,))
    total = cursor.fetchone()[0]
    cursor.close()
    conn.close()
    return total

def atingiu_limite(tabela, user_id):
    if usuario_eh_premium(user_id):
        return False
    return contar_itens(tabela, user_id) >= LIMITE_GRATIS

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        nome = request.form['nome']
        email = request.form['email']
        senha = request.form['senha']

        resposta = requests.post('http://auth_api:5001/register', json={
            'nome': nome,
            'email': email,
            'senha': senha
        })

        if resposta.status_code == 201:
            flash('Cadastro realizado! Faça seu login.', 'success')
            return redirect(url_for('login'))
        else:
            erro = resposta.json().get('erro', 'Erro no cadastro')
            flash(erro, 'danger')

    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        senha = request.form['senha']

        resposta = requests.post('http://auth_api:5001/login', json={
            'email': email,
            'senha': senha
        })

        if resposta.status_code == 200:
            dados = resposta.json()
            session['user_id'] = dados['usuario']['id']
            session['nome'] = dados['usuario']['nome']
            session['user_role'] = dados['usuario']['role']
            registrar_evento(dados['usuario']['id'], 'login')
            flash('Login realizado com sucesso!', 'success')
            return redirect(url_for('index'))
        else:
            erro = resposta.json().get('erro', 'Erro ao fazer login')
            flash(erro, 'danger')

    return render_template('login.html')

@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form['email']

        resposta = requests.post('http://auth_api:5001/forgot-password', json={'email': email})

        if resposta.status_code == 200:
            flash(resposta.json().get('mensagem'), 'success')
            return redirect(url_for('login'))
        else:
            erro = resposta.json().get('erro', 'Erro ao processar solicitação')
            flash(erro, 'danger')

    return render_template('forgot_password.html')

@app.route('/logout')
def logout():
    if 'user_id' in session:
        registrar_evento(session['user_id'], 'logout')
    session.clear()
    return redirect(url_for('index'))

# ---------------------------------------------------------------------------
# CotaAgro: landing, cotações, praças e produtos acompanhados
# ---------------------------------------------------------------------------

UNIDADES_VALIDAS = ('saca 60kg', 'tonelada', 'kg', 'litro')
EXTENSOES_ANEXO = {'pdf', 'png', 'jpg', 'jpeg', 'webp'}
PREFIXO_ANEXOS = 'cotacoes'

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def formatar_data(dt):
    return dt.strftime('%d/%m/%Y %H:%M') if dt else ''

def parse_preco(texto):
    try:
        valor = Decimal((texto or '').strip().replace(',', '.'))
    except InvalidOperation:
        return None
    if valor <= 0 or valor >= Decimal('100000000'):
        return None
    return valor.quantize(Decimal('0.01'))

def pode_gerenciar(praca_usuario_id):
    """Admin gerencia tudo; operador só as cotações das próprias praças."""
    papel = session.get('user_role')
    if papel == 'admin':
        return True
    return papel == 'operador' and praca_usuario_id == session.get('user_id')

def pracas_permitidas(cursor):
    if session.get('user_role') == 'admin':
        cursor.execute("SELECT id, nome FROM pracas ORDER BY nome")
    else:
        cursor.execute("SELECT id, nome FROM pracas WHERE usuario_id = %s ORDER BY nome", (session['user_id'],))
    return cursor.fetchall()

def buscar_cotacao(cursor, cotacao_id):
    cursor.execute("""
        SELECT c.id, c.produto_id, p.nome AS produto_nome, c.praca_id, pr.nome AS praca_nome,
               pr.usuario_id AS usuario_id, c.preco, c.unidade, c.observacao, c.anexo_key,
               c.atualizado_em, u.nome AS publicado_por
        FROM cotacoes c
        JOIN produtos p ON p.id = c.produto_id
        JOIN pracas pr ON pr.id = c.praca_id
        JOIN usuarios u ON u.id = c.usuario_id
        WHERE c.id = %s
    """, (cotacao_id,))
    cot = cursor.fetchone()
    if cot:
        cot['preco'] = float(cot['preco'])
        cot['data_fmt'] = formatar_data(cot['atualizado_em'])
    return cot

def salvar_anexo(arquivo):
    """Envia o anexo (PDF ou imagem) ao MinIO. Retorna (chave, erro)."""
    if not arquivo or arquivo.filename == '':
        return None, None
    if '.' not in arquivo.filename or arquivo.filename.rsplit('.', 1)[1].lower() not in EXTENSOES_ANEXO:
        return None, 'Anexo inválido. Envie PDF, png, jpg, jpeg ou webp.'
    tipo = arquivo.mimetype or ''
    if not (tipo == 'application/pdf' or tipo.startswith('image/')):
        return None, 'Anexo inválido: o tipo de conteúdo não é PDF nem imagem.'
    try:
        garantir_bucket()
        extensao = arquivo.filename.rsplit('.', 1)[1].lower()
        chave = f"{PREFIXO_ANEXOS}/{session['user_id']}/{uuid.uuid4().hex}.{extensao}"
        arquivo.stream.seek(0, os.SEEK_END)
        tamanho = arquivo.stream.tell()
        arquivo.stream.seek(0)
        minio_client.put_object(MINIO_BUCKET, chave, arquivo.stream, length=tamanho, content_type=tipo)
    except S3Error as e:
        return None, f'Erro ao enviar o anexo para o object storage: {e}'
    return chave, None

def remover_anexo(chave):
    if not chave:
        return
    try:
        minio_client.remove_object(MINIO_BUCKET, chave)
    except S3Error:
        pass

def ler_formulario_cotacao(cursor):
    """Valida o formulário. Retorna (dados, erro)."""
    try:
        produto_id = int(request.form.get('produto_id', ''))
        praca_id = int(request.form.get('praca_id', ''))
    except ValueError:
        return None, 'Produto ou praça inválidos.'

    cursor.execute("SELECT nome FROM produtos WHERE id = %s", (produto_id,))
    produto = cursor.fetchone()
    if not produto:
        return None, 'Produto não encontrado.'

    permitidas = {p['id']: p['nome'] for p in pracas_permitidas(cursor)}
    if praca_id not in permitidas:
        registrar_evento(session['user_id'], 'acesso_negado', f'tentou publicar na praça {praca_id}')
        return 'FORBIDDEN', None

    preco = parse_preco(request.form.get('preco'))
    if preco is None:
        return None, 'Preço inválido. Informe um valor maior que zero.'

    unidade = request.form.get('unidade')
    if unidade not in UNIDADES_VALIDAS:
        return None, 'Unidade inválida.'

    return {
        'produto_id': produto_id, 'produto_nome': produto['nome'],
        'praca_id': praca_id, 'praca_nome': permitidas[praca_id],
        'preco': preco, 'unidade': unidade,
        'observacao': request.form.get('observacao', '').strip()[:280] or None,
    }, None

@app.route('/')
def index():
    if 'user_id' in session:
        return redirect('/cotacoes')
    return render_template('landing.html')

@app.route('/cotacoes', methods=['GET'])
@login_required
def cotacoes_lista():
    produto_sel = request.args.get('produto_id', type=int)

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("SELECT id, nome FROM produtos ORDER BY nome")
    produtos = cursor.fetchall()

    consulta = """
        SELECT c.id, c.produto_id, p.nome AS produto_nome, pr.nome AS praca_nome,
               pr.usuario_id AS usuario_id, c.preco, c.unidade, c.atualizado_em
        FROM cotacoes c
        JOIN produtos p ON p.id = c.produto_id
        JOIN pracas pr ON pr.id = c.praca_id
    """
    parametros = ()
    if produto_sel:
        consulta += " WHERE c.produto_id = %s"
        parametros = (produto_sel,)
    consulta += " ORDER BY p.nome, c.preco DESC"
    cursor.execute(consulta, parametros)
    cotacoes = cursor.fetchall()
    for c in cotacoes:
        c['preco'] = float(c['preco'])
        c['data_fmt'] = formatar_data(c['atualizado_em'])

    cursor.execute("SELECT produto_id FROM acompanhados WHERE usuario_id = %s", (session['user_id'],))
    acompanhados = [row['produto_id'] for row in cursor.fetchall()]

    cursor.close()
    conn.close()

    return render_template('cotacoes.html', cotacoes=cotacoes, produtos=produtos,
                           produto_sel=produto_sel, acompanhados=acompanhados)

@app.route('/cotacoes/<int:cotacao_id>', methods=['GET'])
@login_required
def cotacao_detalhe(cotacao_id):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cotacao = buscar_cotacao(cursor, cotacao_id)
    cursor.close()
    conn.close()

    if not cotacao:
        abort(404)

    cotacao['anexo_url'] = url_for('cotacao_anexo', cotacao_id=cotacao_id) if cotacao['anexo_key'] else None
    return render_template('cotacao_detalhe.html', cotacao=cotacao)

@app.route('/cotacoes/<int:cotacao_id>/anexo', methods=['GET'])
@login_required
def cotacao_anexo(cotacao_id):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT anexo_key FROM cotacoes WHERE id = %s", (cotacao_id,))
    linha = cursor.fetchone()
    cursor.close()
    conn.close()

    if not linha or not linha['anexo_key']:
        return ('', 404)

    try:
        objeto = minio_client.get_object(MINIO_BUCKET, linha['anexo_key'])
    except S3Error:
        return ('', 404)

    def gerar():
        try:
            for pedaco in objeto.stream(32 * 1024):
                yield pedaco
        finally:
            objeto.close()
            objeto.release_conn()

    resposta = Response(gerar(), mimetype=objeto.headers.get('Content-Type', 'application/octet-stream'))
    resposta.headers['Cache-Control'] = 'private, max-age=3600'
    resposta.headers['X-Content-Type-Options'] = 'nosniff'
    return resposta

@app.route('/cotacoes/nova', methods=['GET', 'POST'])
@role_required('operador', 'admin')
def cotacao_nova():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        pracas = pracas_permitidas(cursor)
        if not pracas:
            flash('Cadastre a sua praça (cooperativa ou cerealista) antes de publicar cotações.', 'danger')
            return redirect('/pracas/nova')

        if request.method == 'POST':
            dados, erro = ler_formulario_cotacao(cursor)
            if dados == 'FORBIDDEN':
                return jsonify({"erro": "Você só pode publicar cotações nas suas próprias praças."}), 403
            if erro:
                flash(erro, 'danger')
            else:
                chave, erro_anexo = salvar_anexo(request.files.get('anexo'))
                if erro_anexo:
                    flash(erro_anexo, 'danger')
                else:
                    cursor.execute("""
                        INSERT INTO cotacoes (produto_id, praca_id, usuario_id, preco, unidade, observacao, anexo_key)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """, (dados['produto_id'], dados['praca_id'], session['user_id'],
                          dados['preco'], dados['unidade'], dados['observacao'], chave))
                    conn.commit()
                    registrar_evento(session['user_id'], 'cotacao_publicada',
                                     f"{dados['produto_nome']} em {dados['praca_nome']}: R$ {dados['preco']}")
                    flash('Cotação publicada!', 'success')
                    return redirect('/cotacoes')

        cursor.execute("SELECT id, nome FROM produtos ORDER BY nome")
        produtos = cursor.fetchall()
    finally:
        cursor.close()
        conn.close()

    return render_template('cotacao_form.html', cotacao=None, produtos=produtos, pracas=pracas)

@app.route('/cotacoes/<int:cotacao_id>/editar', methods=['GET', 'POST'])
@role_required('operador', 'admin')
def cotacao_editar(cotacao_id):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cotacao = buscar_cotacao(cursor, cotacao_id)
        if not cotacao:
            return jsonify({"erro": "Cotação não encontrada."}), 404
        if not pode_gerenciar(cotacao['usuario_id']):
            registrar_evento(session['user_id'], 'acesso_negado', f'tentou editar a cotação {cotacao_id} de outra praça')
            return jsonify({"erro": "Você só pode editar cotações das suas próprias praças."}), 403

        if request.method == 'POST':
            dados, erro = ler_formulario_cotacao(cursor)
            if dados == 'FORBIDDEN':
                return jsonify({"erro": "Você só pode usar as suas próprias praças."}), 403
            if erro:
                flash(erro, 'danger')
            else:
                chave, erro_anexo = salvar_anexo(request.files.get('anexo'))
                if erro_anexo:
                    flash(erro_anexo, 'danger')
                else:
                    chave_final = chave or cotacao['anexo_key']
                    cursor.execute("""
                        UPDATE cotacoes
                        SET produto_id = %s, praca_id = %s, preco = %s, unidade = %s,
                            observacao = %s, anexo_key = %s, atualizado_em = CURRENT_TIMESTAMP
                        WHERE id = %s
                    """, (dados['produto_id'], dados['praca_id'], dados['preco'], dados['unidade'],
                          dados['observacao'], chave_final, cotacao_id))
                    conn.commit()
                    if chave and cotacao['anexo_key']:
                        remover_anexo(cotacao['anexo_key'])
                    registrar_evento(session['user_id'], 'cotacao_atualizada',
                                     f"{dados['produto_nome']} em {dados['praca_nome']}: R$ {dados['preco']}")
                    flash('Cotação atualizada!', 'success')
                    return redirect('/cotacoes')

        cursor.execute("SELECT id, nome FROM produtos ORDER BY nome")
        produtos = cursor.fetchall()
        pracas = pracas_permitidas(cursor)
    finally:
        cursor.close()
        conn.close()

    return render_template('cotacao_form.html', cotacao=cotacao, produtos=produtos, pracas=pracas)

@app.route('/cotacoes/<int:cotacao_id>/remover', methods=['POST'])
@role_required('operador', 'admin')
def cotacao_remover(cotacao_id):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cotacao = buscar_cotacao(cursor, cotacao_id)
        if not cotacao:
            return jsonify({"erro": "Cotação não encontrada."}), 404
        if not pode_gerenciar(cotacao['usuario_id']):
            registrar_evento(session['user_id'], 'acesso_negado', f'tentou remover a cotação {cotacao_id} de outra praça')
            return jsonify({"erro": "Você só pode remover cotações das suas próprias praças."}), 403

        cursor.execute("DELETE FROM cotacoes WHERE id = %s", (cotacao_id,))
        conn.commit()
    finally:
        cursor.close()
        conn.close()

    remover_anexo(cotacao['anexo_key'])
    registrar_evento(session['user_id'], 'cotacao_removida',
                     f"{cotacao['produto_nome']} em {cotacao['praca_nome']}")
    flash('Cotação removida.', 'success')
    return redirect('/cotacoes')

@app.route('/pracas/nova', methods=['GET', 'POST'])
@role_required('operador', 'admin')
def praca_nova():
    if request.method == 'POST':
        nome = request.form.get('nome', '').strip()[:120]
        cidade = request.form.get('cidade', '').strip()[:80] or None
        if not nome:
            flash('Informe o nome da praça.', 'danger')
        else:
            conn = get_db_connection()
            cursor = conn.cursor()
            try:
                cursor.execute("INSERT INTO pracas (nome, cidade, usuario_id) VALUES (%s, %s, %s)",
                               (nome, cidade, session['user_id']))
                conn.commit()
                registrar_evento(session['user_id'], 'praca_criada', nome)
                flash('Praça cadastrada! Agora você pode publicar cotações.', 'success')
                return redirect('/cotacoes/nova')
            except mysql.connector.IntegrityError:
                flash('Já existe uma praça com esse nome.', 'danger')
            finally:
                cursor.close()
                conn.close()
    return render_template('praca_form.html')

@app.route('/acompanhados', methods=['GET'])
@login_required
def acompanhados_lista():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT p.id, p.nome, p.unidade_padrao AS unidade,
               (SELECT MAX(c.preco) FROM cotacoes c WHERE c.produto_id = p.id) AS melhor_preco
        FROM acompanhados a
        JOIN produtos p ON p.id = a.produto_id
        WHERE a.usuario_id = %s
        ORDER BY a.criado_em DESC
    """, (session['user_id'],))
    produtos = cursor.fetchall()
    cursor.close()
    conn.close()

    for p in produtos:
        p['melhor_preco'] = float(p['melhor_preco']) if p['melhor_preco'] is not None else None

    return render_template('acompanhados.html', produtos=produtos,
                           premium=usuario_eh_premium(session['user_id']), limite=LIMITE_GRATIS)

@app.route('/acompanhados/adicionar', methods=['POST'])
@login_required
def acompanhados_adicionar():
    produto_id = request.form.get('produto_id', type=int)
    if not produto_id:
        flash('Produto inválido.', 'danger')
        return redirect(request.referrer or '/cotacoes')

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT nome FROM produtos WHERE id = %s", (produto_id,))
    produto = cursor.fetchone()
    cursor.execute("SELECT 1 AS ja FROM acompanhados WHERE usuario_id = %s AND produto_id = %s",
                   (session['user_id'], produto_id))
    ja_acompanha = cursor.fetchone()
    cursor.close()
    conn.close()

    if not produto:
        flash('Produto não encontrado.', 'danger')
        return redirect(request.referrer or '/cotacoes')
    if ja_acompanha:
        return redirect(request.referrer or '/cotacoes')

    if atingiu_limite('acompanhados', session['user_id']):
        registrar_evento(session['user_id'], 'limite_atingido', 'acompanhados')
        flash(f'Limite de {LIMITE_GRATIS} produtos acompanhados do plano Free atingido. Assine o Pro para acompanhar sem limite.', 'danger')
        return redirect(url_for('perfil'))

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO acompanhados (usuario_id, produto_id) VALUES (%s, %s)",
                       (session['user_id'], produto_id))
        conn.commit()
        registrar_evento(session['user_id'], 'produto_acompanhado', produto['nome'])
        flash(f"Você agora acompanha {produto['nome']}.", 'success')
    except mysql.connector.IntegrityError:
        pass
    finally:
        cursor.close()
        conn.close()
    return redirect(request.referrer or '/cotacoes')

@app.route('/acompanhados/remover', methods=['POST'])
@login_required
def acompanhados_remover():
    produto_id = request.form.get('produto_id', type=int)

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM acompanhados WHERE usuario_id = %s AND produto_id = %s",
                   (session['user_id'], produto_id))
    conn.commit()
    cursor.close()
    conn.close()

    registrar_evento(session['user_id'], 'produto_desacompanhado', str(produto_id))
    return redirect(request.referrer or '/acompanhados')

@app.route('/perfil', methods=['GET'])
def perfil():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT nome, email, bio, foto_key, premium FROM usuarios WHERE id = %s", (session['user_id'],))
    usuario = cursor.fetchone()

    cursor.execute("""
        SELECT p.id, p.nome
        FROM acompanhados a
        JOIN produtos p ON p.id = a.produto_id
        WHERE a.usuario_id = %s
        ORDER BY a.criado_em DESC
    """, (session['user_id'],))
    acompanhados = cursor.fetchall()
    cursor.close()
    conn.close()

    foto_url = None
    if usuario and usuario.get('foto_key'):
        versao = usuario['foto_key'].rsplit('/', 1)[-1].split('.')[0]
        foto_url = url_for('foto_perfil', usuario_id=session['user_id'], v=versao)

    return render_template('perfil.html', usuario=usuario, acompanhados=acompanhados,
                           foto_url=foto_url, limite=LIMITE_GRATIS)

@app.route('/foto/<int:usuario_id>', methods=['GET'])
def foto_perfil(usuario_id):
    if 'user_id' not in session:
        return ('', 401)

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT foto_key FROM usuarios WHERE id = %s", (usuario_id,))
    linha = cursor.fetchone()
    cursor.close()
    conn.close()

    if not linha or not linha['foto_key']:
        return ('', 404)

    try:
        objeto = minio_client.get_object(MINIO_BUCKET, linha['foto_key'])
    except S3Error:
        return ('', 404)

    def gerar():
        try:
            for pedaco in objeto.stream(32 * 1024):
                yield pedaco
        finally:
            objeto.close()
            objeto.release_conn()

    resposta = Response(gerar(), mimetype=objeto.headers.get('Content-Type', 'application/octet-stream'))
    resposta.headers['Cache-Control'] = 'private, max-age=86400'
    resposta.headers['X-Content-Type-Options'] = 'nosniff'
    return resposta

@app.route('/perfil/bio', methods=['POST'])
def atualizar_bio():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    bio = request.form.get('bio', '').strip()[:280]

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE usuarios SET bio = %s WHERE id = %s", (bio, session['user_id']))
    conn.commit()
    cursor.close()
    conn.close()

    registrar_evento(session['user_id'], 'atualizar_bio')
    flash('Bio atualizada.', 'success')
    return redirect(url_for('perfil'))

@app.route('/perfil/foto', methods=['POST'])
def atualizar_foto():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    arquivo = request.files.get('foto')
    if not arquivo or arquivo.filename == '':
        flash('Nenhum arquivo selecionado.', 'danger')
        return redirect(url_for('perfil'))

    if not extensao_valida(arquivo.filename):
        flash('Formato inválido. Envie uma imagem (png, jpg, jpeg, gif ou webp).', 'danger')
        return redirect(url_for('perfil'))

    if not (arquivo.mimetype or '').startswith('image/'):
        flash('Arquivo inválido: o tipo de conteúdo não é uma imagem.', 'danger')
        return redirect(url_for('perfil'))

    try:
        garantir_bucket()

        extensao = arquivo.filename.rsplit('.', 1)[1].lower()
        chave = f"{session['user_id']}/{uuid.uuid4().hex}.{extensao}"

        arquivo.stream.seek(0, os.SEEK_END)
        tamanho = arquivo.stream.tell()
        arquivo.stream.seek(0)

        minio_client.put_object(
            MINIO_BUCKET, chave, arquivo.stream, length=tamanho, content_type=arquivo.mimetype
        )
    except S3Error as e:
        flash(f'Erro ao enviar a imagem para o object storage: {e}', 'danger')
        return redirect(url_for('perfil'))

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE usuarios SET foto_key = %s WHERE id = %s", (chave, session['user_id']))
    conn.commit()
    cursor.close()
    conn.close()

    registrar_evento(session['user_id'], 'atualizar_foto_perfil', chave)
    flash('Foto de perfil atualizada!', 'success')
    return redirect(url_for('perfil'))

def ativar_premium_se_valido(cs, user_id):
    if cs.get('client_reference_id') != str(user_id):
        return False
    if cs.get('mode') != 'subscription' or cs.get('payment_status') != 'paid':
        return False
    sub_id = cs.get('subscription')
    if not sub_id:
        return False
    sub = stripe.Subscription.retrieve(sub_id)
    if sub.get('status') not in ('active', 'trialing'):
        return False

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE usuarios SET premium = TRUE, stripe_customer_id = %s, stripe_subscription_id = %s WHERE id = %s",
        (cs.get('customer'), sub_id, user_id)
    )
    conn.commit()
    cursor.close()
    conn.close()
    registrar_evento(user_id, 'premium_ativado', sub_id)
    return True

@app.route('/premium/assinar', methods=['POST'])
def premium_assinar():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    if usuario_eh_premium(session['user_id']):
        flash('Você já é Pro.', 'success')
        return redirect(url_for('perfil'))

    if not (stripe.api_key or '').startswith('sk_test_'):
        flash(
            'O Stripe não está configurado em modo de teste. Configure STRIPE_SECRET_KEY '
            'com uma chave sk_test_ e STRIPE_PRICE_ID com um preço criado no modo de teste '
            'para usar o cartão 4242.',
            'danger'
        )
        return redirect(url_for('perfil'))

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT email FROM usuarios WHERE id = %s", (session['user_id'],))
    email = cursor.fetchone()[0]
    cursor.close()
    conn.close()

    try:
        checkout = stripe.checkout.Session.create(
            mode='subscription',
            line_items=[{'price': STRIPE_PRICE_ID, 'quantity': 1}],
            client_reference_id=str(session['user_id']),
            customer_email=email,
            locale='pt-BR',
            success_url=f'{BASE_URL}/premium/sucesso?session_id={{CHECKOUT_SESSION_ID}}',
            cancel_url=f'{BASE_URL}/perfil',
        )
    except stripe.StripeError as e:
        flash(f'Erro ao iniciar o checkout: {e}', 'danger')
        return redirect(url_for('perfil'))

    registrar_evento(session['user_id'], 'checkout_iniciado')
    return redirect(checkout.url, code=303)

@app.route('/premium/sucesso', methods=['GET'])
def premium_sucesso():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    sid = request.args.get('session_id')
    if sid:
        try:
            cs = stripe.checkout.Session.retrieve(sid)
            if ativar_premium_se_valido(cs, session['user_id']):
                flash('Pagamento confirmado! Você agora é Pro.', 'success')
                return redirect(url_for('perfil'))
        except stripe.StripeError:
            pass

    flash('Pagamento recebido. Se o plano Pro ainda não apareceu, clique em "Já paguei, atualizar status" no perfil.', 'success')
    return redirect(url_for('perfil'))

@app.route('/premium/sincronizar', methods=['POST'])
def premium_sincronizar():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    try:
        sessoes = stripe.checkout.Session.list(limit=50)
        for cs in sessoes.data:
            if ativar_premium_se_valido(cs, session['user_id']):
                flash('Plano Pro ativado! Pagamento confirmado no Stripe.', 'success')
                return redirect(url_for('perfil'))
    except stripe.StripeError as e:
        flash(f'Não foi possível consultar o Stripe: {e}', 'danger')
        return redirect(url_for('perfil'))

    flash('Nenhum pagamento ativo encontrado para a sua conta.', 'danger')
    return redirect(url_for('perfil'))

@app.route('/webhook/stripe', methods=['POST'])
def webhook_stripe():
    payload = request.get_data()
    assinatura = request.headers.get('Stripe-Signature', '')

    try:
        stripe.Webhook.construct_event(payload, assinatura, STRIPE_WEBHOOK_SECRET)
    except ValueError:
        return jsonify({"erro": "Payload inválido"}), 400
    except stripe.SignatureVerificationError:
        return jsonify({"erro": "Assinatura inválida"}), 400

    evento = json.loads(payload)
    tipo = evento.get('type')
    obj = evento.get('data', {}).get('object', {})

    if tipo == 'checkout.session.completed' and obj.get('payment_status') == 'paid':
        usuario_id = obj.get('client_reference_id')
        if usuario_id:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE usuarios SET premium = TRUE, stripe_customer_id = %s, stripe_subscription_id = %s WHERE id = %s",
                (obj.get('customer'), obj.get('subscription'), int(usuario_id))
            )
            conn.commit()
            cursor.close()
            conn.close()
            registrar_evento(int(usuario_id), 'premium_ativado', obj.get('subscription') or '')

    elif tipo == 'customer.subscription.deleted':
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE usuarios SET premium = FALSE WHERE stripe_subscription_id = %s", (obj.get('id'),))
        conn.commit()
        cursor.close()
        conn.close()

    return jsonify({"recebido": True}), 200

@app.route('/admin/usuarios', methods=['GET'])
@admin_required
def admin_usuarios():
    resposta = requests.get('http://auth_api:5001/usuarios')
    usuarios = resposta.json().get('usuarios', [])
    return render_template('admin_usuarios.html', usuarios=usuarios)

@app.route('/admin/usuarios/<int:usuario_id>/role', methods=['POST'])
@admin_required
def admin_alterar_role(usuario_id):
    novo_role = request.form.get('role')

    if novo_role not in ROLES_VALIDOS:
        flash("Papel inválido. Use produtor, operador ou admin.", 'danger')
        return redirect(url_for('admin_usuarios'))

    resposta = requests.put(
        f'http://auth_api:5001/usuarios/{usuario_id}/role',
        json={'role': novo_role}
    )

    if resposta.status_code == 200:
        flash(resposta.json().get('mensagem', 'Papel atualizado.'), 'success')
    else:
        flash(resposta.json().get('erro', 'Erro ao atualizar papel'), 'danger')

    return redirect(url_for('admin_usuarios'))

@app.route('/admin/usuarios/<int:usuario_id>/deletar', methods=['POST'])
@admin_required
def admin_deletar_usuario(usuario_id):
    if usuario_id == session['user_id']:
        flash('Você não pode apagar a própria conta enquanto estiver logado.', 'danger')
        return redirect(url_for('admin_usuarios'))

    resposta = requests.delete(f'http://auth_api:5001/usuarios/{usuario_id}')

    if resposta.status_code == 200:
        flash(resposta.json().get('mensagem', 'Usuário apagado.'), 'success')
    else:
        flash(resposta.json().get('erro', 'Erro ao apagar usuário'), 'danger')

    return redirect(url_for('admin_usuarios'))

@app.route('/admin/metricas', methods=['GET'])
@admin_required
def admin_metricas():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("SELECT COUNT(*) AS total FROM usuarios")
    total_usuarios = cursor.fetchone()['total']

    cursor.execute("SELECT COUNT(*) AS total FROM cotacoes")
    total_cotacoes = cursor.fetchone()['total']

    cursor.execute("SELECT COUNT(*) AS total FROM pracas")
    total_pracas = cursor.fetchone()['total']

    cursor.execute("SELECT COUNT(*) AS total FROM acompanhados")
    total_acompanhamentos = cursor.fetchone()['total']

    cursor.execute("SELECT role, COUNT(*) AS total FROM usuarios GROUP BY role")
    usuarios_por_papel = cursor.fetchall()

    cursor.close()
    conn.close()

    metricas = {
        "total_usuarios": total_usuarios,
        "total_cotacoes": total_cotacoes,
        "total_pracas": total_pracas,
        "total_acompanhamentos": total_acompanhamentos,
        "usuarios_por_papel": usuarios_por_papel,
    }

    return render_template('admin_metricas.html', metricas=metricas)

@app.route('/admin/logs', methods=['GET'])
@admin_required
def admin_logs():
    n = request.args.get('n', default=50, type=int)

    try:
        resposta = requests.get(f'http://log_service:5002/eventos', params={'n': n}, timeout=3)
        eventos = resposta.json().get('eventos', [])
    except requests.exceptions.RequestException:
        eventos = []
        flash('Não foi possível consultar o log-service.', 'danger')

    for ev in eventos:
        try:
            ev['timestamp_fmt'] = datetime.fromtimestamp(float(ev['timestamp']), tz=BRASILIA_TZ).strftime('%d/%m/%Y %H:%M:%S')
        except (KeyError, ValueError, TypeError):
            ev['timestamp_fmt'] = ev.get('timestamp', '')

    return render_template('admin_logs.html', eventos=eventos, n=n)

@app.route('/reset-password', methods=['GET', 'POST'])
def reset_password():
    token = request.args.get('token') or request.form.get('token')

    if not token:
        flash('Token de recuperação não fornecido.', 'danger')
        return redirect(url_for('login'))

    if request.method == 'POST':
        nova_senha = request.form.get('senha')

        resposta = requests.post('http://auth_api:5001/reset-password', json={
            'token': token,
            'senha': nova_senha
        })

        if resposta.status_code == 200:
            flash(resposta.json().get('mensagem'), 'success')
            return redirect(url_for('login'))
        else:
            erro = resposta.json().get('erro', 'Erro ao redefinir senha')
            flash(erro, 'danger')

    return render_template('reset_password.html', token=token)