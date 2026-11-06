from flask import Flask, request, jsonify
import mysql.connector
from mysql.connector import pooling
import os
from werkzeug.security import generate_password_hash, check_password_hash
import secrets
from datetime import datetime, timedelta
import smtplib
from email.mime.text import MIMEText

app = Flask(__name__)

ROLES_VALIDOS = ('produtor', 'operador', 'admin')

db_pool = pooling.MySQLConnectionPool(
    pool_name="auth_pool",
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

def enviar_email_recuperacao(email_destino, token):
    link_recuperacao = f"http://pedro-ferreira-isw055.lapps.studio/reset-password?token={token}"

    corpo_email = f"""Olá,

Você solicitou a recuperação de senha.
Clique no link abaixo para redefinir:
{link_recuperacao}

Este link expira em 30 minutos.
Se você não solicitou, apenas ignore este e-mail.
"""

    mensagem = MIMEText(corpo_email)
    mensagem['Subject'] = 'Recuperação de Senha - CotaAgro'
    mensagem['From'] = os.getenv('MAIL_FROM', 'seguranca@cotaagro.com')
    mensagem['To'] = email_destino

    try:
        usa_ssl = os.getenv('MAIL_USE_SSL') == 'True'
        if usa_ssl:
            with smtplib.SMTP_SSL(os.getenv('MAIL_SERVER'), int(os.getenv('MAIL_PORT'))) as server:
                server.login(os.getenv('MAIL_USERNAME'), os.getenv('MAIL_PASSWORD'))
                server.send_message(mensagem)
        else:
            with smtplib.SMTP(os.getenv('MAIL_SERVER'), int(os.getenv('MAIL_PORT'))) as server:
                if os.getenv('MAIL_USE_TLS') == 'True':
                    server.starttls()
                server.login(os.getenv('MAIL_USERNAME'), os.getenv('MAIL_PASSWORD'))
                server.send_message(mensagem)
        return True
    except Exception as e:
        print(f"Erro SMTP: {e}")
        return False

@app.route('/status', methods=['GET'])
def status():
    return jsonify({"servico": "auth_api", "status": "online"}), 200

@app.route('/register', methods=['POST'])
def register():
    dados = request.get_json()
    nome = dados.get('nome')
    email = dados.get('email')
    senha = dados.get('senha')

    if not nome or not email or not senha:
        return jsonify({"erro": "Dados incompletos"}), 400

    senha_hash = generate_password_hash(senha)

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('INSERT INTO usuarios (nome, email, senha_hash, role) VALUES (%s, %s, %s, %s)',
                       (nome, email, senha_hash, 'produtor'))
        conn.commit()
        cursor.close()
        conn.close()
        return jsonify({"mensagem": "Usuário registrado com sucesso!"}), 201
    except mysql.connector.IntegrityError:
        return jsonify({"erro": "Email já cadastrado"}), 409
    except Exception as e:
        return jsonify({"erro": str(e)}), 500

@app.route('/login', methods=['POST'])
def login():
    dados = request.get_json()
    email = dados.get('email')
    senha = dados.get('senha')

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute('SELECT * FROM usuarios WHERE email = %s', (email,))
    usuario = cursor.fetchone()
    cursor.close()
    conn.close()

    if usuario and check_password_hash(usuario['senha_hash'], senha):
        return jsonify({
            "mensagem": "Login aprovado",
            "usuario": {
                "id": usuario['id'],
                "nome": usuario['nome'],
                "role": usuario['role']
            }
        }), 200
    else:
        return jsonify({"erro": "Credenciais inválidas"}), 401

@app.route('/usuarios', methods=['GET'])
def listar_usuarios():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute('SELECT id, nome, email, role FROM usuarios ORDER BY id')
    usuarios = cursor.fetchall()
    cursor.close()
    conn.close()
    return jsonify({"usuarios": usuarios}), 200

@app.route('/usuarios/<int:usuario_id>/role', methods=['PUT'])
def atualizar_role(usuario_id):
    dados = request.get_json()
    novo_role = dados.get('role')

    if novo_role not in ROLES_VALIDOS:
        return jsonify({"erro": "role inválido. Use 'produtor', 'operador' ou 'admin'."}), 400

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute('SELECT id FROM usuarios WHERE id = %s', (usuario_id,))
    if not cursor.fetchone():
        cursor.close()
        conn.close()
        return jsonify({"erro": "Usuário não encontrado"}), 404

    cursor.execute('UPDATE usuarios SET role = %s WHERE id = %s', (novo_role, usuario_id))
    conn.commit()
    cursor.close()
    conn.close()

    return jsonify({"mensagem": f"Papel atualizado para '{novo_role}'."}), 200

@app.route('/usuarios/<int:usuario_id>', methods=['DELETE'])
def deletar_usuario(usuario_id):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute('SELECT id FROM usuarios WHERE id = %s', (usuario_id,))
    if not cursor.fetchone():
        cursor.close()
        conn.close()
        return jsonify({"erro": "Usuário não encontrado"}), 404

    cursor.execute('DELETE FROM acompanhados WHERE usuario_id = %s', (usuario_id,))
    cursor.execute('DELETE FROM cotacoes WHERE usuario_id = %s', (usuario_id,))
    cursor.execute('DELETE FROM pracas WHERE usuario_id = %s', (usuario_id,))
    cursor.execute('DELETE FROM reset_tokens WHERE usuario_id = %s', (usuario_id,))
    cursor.execute('DELETE FROM usuarios WHERE id = %s', (usuario_id,))
    conn.commit()
    cursor.close()
    conn.close()

    return jsonify({"mensagem": "Usuário apagado com sucesso."}), 200

@app.route('/forgot-password', methods=['POST'])
def forgot_password():
    dados = request.get_json()
    email = dados.get('email')

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute('SELECT id FROM usuarios WHERE email = %s', (email,))
    usuario = cursor.fetchone()

    if not usuario:
        cursor.close()
        conn.close()
        return jsonify({"mensagem": "Se o e-mail existir, um link de recuperação será enviado."}), 200

    token = secrets.token_hex(32)
    agora = datetime.now()
    expira_em = agora + timedelta(minutes=30)

    cursor.execute('''
        INSERT INTO reset_tokens (token, usuario_id, criado_em, expira_em)
        VALUES (%s, %s, %s, %s)
    ''', (token, usuario['id'], agora, expira_em))
    conn.commit()
    cursor.close()
    conn.close()

    if enviar_email_recuperacao(email, token):
        return jsonify({"mensagem": "Se o e-mail existir, um link de recuperação será enviado."}), 200
    else:
        return jsonify({"erro": "Falha ao disparar o e-mail."}), 500

@app.route('/reset-password', methods=['POST'])
def reset_password():
    dados = request.get_json()
    token = dados.get('token')
    nova_senha = dados.get('senha')

    if not token or not nova_senha:
        return jsonify({"erro": "Dados incompletos"}), 400

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute('''
            SELECT * FROM reset_tokens
            WHERE token = %s AND usado = FALSE AND expira_em > NOW()
        ''', (token,))
        registro = cursor.fetchone()

        if not registro:
            cursor.close()
            conn.close()
            return jsonify({"erro": "Link inválido ou expirado"}), 400

        senha_hash = generate_password_hash(nova_senha)

        cursor.execute('UPDATE usuarios SET senha_hash = %s WHERE id = %s', (senha_hash, registro['usuario_id']))
        cursor.execute('UPDATE reset_tokens SET usado = TRUE WHERE token = %s', (token,))

        conn.commit()
        cursor.close()
        conn.close()

        return jsonify({"mensagem": "Senha redefinida com sucesso! Faça login com a nova senha."}), 200

    except Exception as e:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()
        return jsonify({"erro": str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001)