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
    if not dt:
        return ''
    return dt.strftime('%d/%m/%Y %H:%M') if hasattr(dt, 'strftime') else str(dt)

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
        return render_template('limite.html', limite=LIMITE_GRATIS), 403

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
    cursor.execute("SELECT nome, email, bio, foto_key, premium, plano, status_assinatura, stripe_customer_id FROM usuarios WHERE id = %s", (session['user_id'],))
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

STATUS_ATIVOS = ('active', 'trialing')

def buscar_usuario_stripe(cursor, customer_id=None, usuario_id=None):
    """Acha o usuário dono do customer do Stripe (ou pelo id guardado nos metadados)."""
    if customer_id:
        cursor.execute("SELECT id, stripe_subscription_id FROM usuarios WHERE stripe_customer_id = %s", (customer_id,))
        usuario = cursor.fetchone()
        if usuario:
            return usuario
    if usuario_id and str(usuario_id).isdigit():
        cursor.execute("SELECT id, stripe_subscription_id FROM usuarios WHERE id = %s", (int(usuario_id),))
        return cursor.fetchone()
    return None

def valor_mensal_centavos(assinatura):
    """Soma o valor mensal dos itens da assinatura (preço x quantidade)."""
    total = 0
    for item in (assinatura.get('items') or {}).get('data', []):
        preco = item.get('price') or {}
        total += int(preco.get('unit_amount') or 0) * int(item.get('quantity') or 1)
    return total or None

def aplicar_evento_stripe(cursor, tipo, obj):
    """Atualiza o plano no banco. Retorna (usuario_id, acao, detalhe) para o log, ou None."""
    if tipo == 'checkout.session.completed':
        if obj.get('mode') != 'subscription' or obj.get('payment_status') not in ('paid', 'no_payment_required'):
            return None
        usuario = buscar_usuario_stripe(cursor, obj.get('customer'), obj.get('client_reference_id'))
        if not usuario:
            return None
        cursor.execute("""
            UPDATE usuarios
            SET plano = 'pro', premium = TRUE, status_assinatura = 'active',
                stripe_customer_id = %s, stripe_subscription_id = %s,
                assinatura_valor_centavos = COALESCE(%s, assinatura_valor_centavos)
            WHERE id = %s
        """, (obj.get('customer'), obj.get('subscription'), obj.get('amount_total'), usuario['id']))
        return usuario['id'], 'assinatura_ativada', obj.get('subscription') or ''

    if tipo == 'customer.subscription.updated':
        metadados = obj.get('metadata') or {}
        usuario = buscar_usuario_stripe(cursor, obj.get('customer'), metadados.get('usuario_id'))
        if not usuario:
            return None
        if usuario['stripe_subscription_id'] and usuario['stripe_subscription_id'] != obj.get('id'):
            return None  # evento de uma assinatura antiga
        status = obj.get('status')
        ativo = status in STATUS_ATIVOS
        cursor.execute("""
            UPDATE usuarios
            SET plano = %s, premium = %s, status_assinatura = %s,
                stripe_customer_id = %s, stripe_subscription_id = %s, assinatura_valor_centavos = %s
            WHERE id = %s
        """, ('pro' if ativo else 'free', ativo, status, obj.get('customer'), obj.get('id'),
              valor_mensal_centavos(obj) if ativo else None, usuario['id']))
        return usuario['id'], 'assinatura_atualizada', f"{status}"

    if tipo == 'customer.subscription.deleted':
        usuario = buscar_usuario_stripe(cursor, obj.get('customer'), (obj.get('metadata') or {}).get('usuario_id'))
        if not usuario:
            return None
        if usuario['stripe_subscription_id'] and usuario['stripe_subscription_id'] != obj.get('id'):
            return None
        cursor.execute("""
            UPDATE usuarios
            SET plano = 'free', premium = FALSE, status_assinatura = 'canceled',
                stripe_subscription_id = NULL, assinatura_valor_centavos = NULL
            WHERE id = %s
        """, (usuario['id'],))
        return usuario['id'], 'assinatura_cancelada', obj.get('id') or ''

    if tipo == 'invoice.payment_failed':
        usuario = buscar_usuario_stripe(cursor, obj.get('customer'))
        if not usuario:
            return None
        cursor.execute("""
            UPDATE usuarios
            SET plano = 'free', premium = FALSE, status_assinatura = 'past_due',
                assinatura_valor_centavos = NULL
            WHERE id = %s
        """, (usuario['id'],))
        return usuario['id'], 'pagamento_falhou', obj.get('id') or ''

    return None

@app.route('/premium/assinar', methods=['POST'])
@login_required
def premium_assinar():
    user_id = session['user_id']

    if usuario_eh_premium(user_id):
        flash('Você já é Pro. Use "Gerenciar assinatura" para trocar de plano ou cancelar.', 'success')
        return redirect(url_for('perfil'))

    if not (stripe.api_key or '').startswith('sk_test_') or not STRIPE_PRICE_ID:
        flash(
            'O Stripe não está configurado em modo de teste. Configure STRIPE_SECRET_KEY '
            '(sk_test_) e STRIPE_PRICE_ID (preço recorrente mensal criado no modo de teste).',
            'danger'
        )
        return redirect(url_for('perfil'))

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT email, stripe_customer_id FROM usuarios WHERE id = %s", (user_id,))
    usuario = cursor.fetchone()

    def abrir_checkout(customer_id):
        return stripe.checkout.Session.create(
            mode='subscription',
            customer=customer_id,
            line_items=[{'price': STRIPE_PRICE_ID, 'quantity': 1}],
            client_reference_id=str(user_id),
            subscription_data={'metadata': {'usuario_id': str(user_id)}},
            locale='pt-BR',
            success_url=f'{BASE_URL}/premium/sucesso',
            cancel_url=f'{BASE_URL}/perfil',
        )

    try:
        customer_id = usuario['stripe_customer_id']
        checkout = None
        if customer_id:
            try:
                checkout = abrir_checkout(customer_id)
            except stripe.StripeError as e:
                # Customer de outra conta/ambiente do Stripe (dado antigo): cria um novo.
                if 'No such customer' not in str(e):
                    raise
                customer_id = None
        if not customer_id:
            cliente = stripe.Customer.create(email=usuario['email'], metadata={'usuario_id': str(user_id)})
            customer_id = cliente.id
            cursor.execute("UPDATE usuarios SET stripe_customer_id = %s WHERE id = %s", (customer_id, user_id))
            conn.commit()
            checkout = abrir_checkout(customer_id)
    except stripe.StripeError as e:
        flash(f'Erro ao iniciar o checkout: {e}', 'danger')
        return redirect(url_for('perfil'))
    finally:
        cursor.close()
        conn.close()

    registrar_evento(user_id, 'checkout_iniciado')
    return redirect(checkout.url, code=303)

@app.route('/premium/sucesso', methods=['GET'])
@login_required
def premium_sucesso():
    # O plano NÃO muda aqui: quem muda é o webhook do Stripe, validado no servidor.
    flash('Pagamento recebido! Estamos aguardando a confirmação do Stripe; o plano Pro aparece assim que ela chegar (alguns segundos).', 'success')
    return redirect(url_for('perfil'))

@app.route('/assinatura/portal', methods=['POST'])
@login_required
def assinatura_portal():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT stripe_customer_id FROM usuarios WHERE id = %s", (session['user_id'],))
    usuario = cursor.fetchone()
    cursor.close()
    conn.close()

    if not usuario or not usuario['stripe_customer_id']:
        flash('Você ainda não tem uma assinatura para gerenciar.', 'danger')
        return redirect(url_for('perfil'))

    try:
        portal = stripe.billing_portal.Session.create(
            customer=usuario['stripe_customer_id'],
            return_url=f'{BASE_URL}/perfil',
        )
    except stripe.StripeError as e:
        if 'No such customer' in str(e):
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("UPDATE usuarios SET stripe_customer_id = NULL, stripe_subscription_id = NULL WHERE id = %s", (session['user_id'],))
            conn.commit()
            cursor.close()
            conn.close()
            flash('Essa assinatura não existe mais no Stripe desta conta (dado antigo). O vínculo foi removido; assine o Pro de novo para gerenciar pelo portal.', 'danger')
        else:
            flash(f'Não foi possível abrir o portal do cliente: {e}', 'danger')
        return redirect(url_for('perfil'))

    registrar_evento(session['user_id'], 'portal_aberto')
    return redirect(portal.url, code=303)

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
    event_id = evento.get('id')
    tipo = evento.get('type')
    obj = evento.get('data', {}).get('object', {})

    if not event_id:
        return jsonify({"erro": "Evento sem id"}), 400

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    resultado = None
    try:
        # Reivindica o evento: se o mesmo id chegar de novo, é ignorado (idempotência).
        try:
            cursor.execute("INSERT INTO stripe_eventos (event_id, tipo) VALUES (%s, %s)", (event_id, tipo))
            conn.commit()
        except mysql.connector.IntegrityError:
            return jsonify({"recebido": True, "duplicado": True}), 200

        try:
            resultado = aplicar_evento_stripe(cursor, tipo, obj)
            conn.commit()
        except Exception as e:
            conn.rollback()
            cursor.execute("DELETE FROM stripe_eventos WHERE event_id = %s", (event_id,))
            conn.commit()
            print(f"Erro ao processar webhook {tipo}: {e}")
            return jsonify({"erro": "Falha ao processar o evento"}), 500
    finally:
        cursor.close()
        conn.close()

    if resultado:
        registrar_evento(resultado[0], resultado[1], resultado[2])

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

@app.route('/admin/assinaturas', methods=['GET'])
@admin_required
def admin_assinaturas():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("SELECT plano, COUNT(*) AS total FROM usuarios GROUP BY plano")
    por_plano = {linha['plano']: linha['total'] for linha in cursor.fetchall()}

    cursor.execute("""
        SELECT COUNT(*) AS total, COALESCE(SUM(assinatura_valor_centavos), 0) AS centavos
        FROM usuarios
        WHERE status_assinatura IN ('active', 'trialing')
    """)
    ativas = cursor.fetchone()

    cursor.execute("SELECT tipo, recebido_em FROM stripe_eventos ORDER BY recebido_em DESC, event_id DESC LIMIT 10")
    eventos_stripe = cursor.fetchall()
    cursor.close()
    conn.close()

    for ev in eventos_stripe:
        ev['recebido_fmt'] = formatar_data(ev['recebido_em'])

    try:
        resposta = requests.get('http://log_service:5002/eventos', params={'n': 10}, timeout=3)
        eventos_log = resposta.json().get('eventos', [])
    except requests.exceptions.RequestException:
        eventos_log = []
        flash('Não foi possível consultar o log-service.', 'danger')

    for ev in eventos_log:
        try:
            ev['timestamp_fmt'] = datetime.fromtimestamp(float(ev['timestamp']), tz=BRASILIA_TZ).strftime('%d/%m/%Y %H:%M:%S')
        except (KeyError, ValueError, TypeError):
            ev['timestamp_fmt'] = ev.get('timestamp', '')

    resumo = {
        'usuarios_free': por_plano.get('free', 0),
        'usuarios_pro': por_plano.get('pro', 0),
        'assinaturas_ativas': ativas['total'],
        'mrr': float(ativas['centavos']) / 100,
    }

    return render_template('admin_assinaturas.html', resumo=resumo,
                           eventos_stripe=eventos_stripe, eventos_log=eventos_log)

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