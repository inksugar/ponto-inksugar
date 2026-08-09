import os
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime, date, timedelta
from functools import wraps
from zoneinfo import ZoneInfo

import psycopg2
import psycopg2.extras
from flask import Flask, render_template, request, redirect, url_for, session

TZ = ZoneInfo("America/Sao_Paulo")
DIAS = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]
ALMOCO_PADRAO = 30
JORNADA_LIMITE = 420  # 7h em minutos

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "troque-isso")

DATABASE_URL = os.environ["DATABASE_URL"]
ADMIN_SENHA = os.environ.get("ADMIN_SENHA", "inksugar")
CRON_TOKEN = os.environ.get("CRON_TOKEN", "")
WHATS_PHONE = os.environ.get("WHATS_PHONE", "")
WHATS_APIKEY = os.environ.get("WHATS_APIKEY", "")


@contextmanager
def db():
    conn = psycopg2.connect(DATABASE_URL, sslmode="require")
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS funcionarios (
                id SERIAL PRIMARY KEY,
                nome TEXT NOT NULL,
                cargo TEXT NOT NULL DEFAULT '',
                valor_hora NUMERIC(10,2) NOT NULL DEFAULT 0,
                foto TEXT,
                ativo BOOLEAN NOT NULL DEFAULT TRUE
            );
            CREATE TABLE IF NOT EXISTS pontos (
                id SERIAL PRIMARY KEY,
                funcionario_id INTEGER NOT NULL REFERENCES funcionarios(id) ON DELETE CASCADE,
                dia DATE NOT NULL,
                entrada TIMESTAMP,
                saida TIMESTAMP,
                almoco_min INTEGER NOT NULL DEFAULT 0,
                minutos INTEGER
            );
            CREATE TABLE IF NOT EXISTS fechamentos (
                id SERIAL PRIMARY KEY,
                funcionario_id INTEGER NOT NULL REFERENCES funcionarios(id) ON DELETE CASCADE,
                ini DATE NOT NULL,
                fim DATE NOT NULL,
                minutos INTEGER NOT NULL,
                valor NUMERIC(10,2) NOT NULL,
                criado_em TIMESTAMP NOT NULL DEFAULT NOW(),
                UNIQUE (funcionario_id, ini, fim)
            );
            CREATE INDEX IF NOT EXISTS idx_pontos_dia ON pontos (dia);
        """)


def agora():
    return datetime.now(TZ).replace(tzinfo=None)


def hoje():
    return datetime.now(TZ).date()


def hm(minutos):
    if minutos is None:
        return "—"
    minutos = int(minutos)
    return f"{minutos // 60}h{minutos % 60:02d}"


def brl(v):
    return f"R$ {float(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def semana_de(d):
    ini = d - timedelta(days=d.weekday())
    return ini, ini + timedelta(days=6)


def iniciais(nome):
    p = [x for x in nome.split() if x]
    if not p:
        return "?"
    return (p[0][0] + (p[-1][0] if len(p) > 1 else "")).upper()


app.jinja_env.globals.update(hm=hm, brl=brl, iniciais=iniciais)


def calcula(entrada, saida, almoco):
    bruto = int((saida - entrada).total_seconds() // 60)
    trabalhado = max(0, bruto - almoco)
    ajustado = False
    if trabalhado >= JORNADA_LIMITE and almoco < ALMOCO_PADRAO:
        almoco = ALMOCO_PADRAO
        trabalhado = max(0, bruto - almoco)
        ajustado = True
    return almoco, trabalhado, ajustado


# ---------------- Telas da equipe ----------------

@app.route("/")
def home():
    return redirect(url_for("ponto"))


@app.route("/ponto")
def ponto():
    with db() as conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("SELECT * FROM funcionarios WHERE ativo ORDER BY nome")
        equipe = cur.fetchall()
    return render_template("ponto.html", equipe=equipe)


def registro_aberto(cur, fid):
    cur.execute(
        "SELECT * FROM pontos WHERE funcionario_id=%s AND saida IS NULL ORDER BY entrada DESC LIMIT 1",
        (fid,),
    )
    return cur.fetchone()


@app.route("/ponto/<int:fid>")
def pessoa(fid):
    with db() as conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("SELECT * FROM funcionarios WHERE id=%s AND ativo", (fid,))
        f = cur.fetchone()
        if not f:
            return redirect(url_for("ponto"))
        aberto = registro_aberto(cur, fid)
        cur.execute(
            "SELECT * FROM pontos WHERE funcionario_id=%s AND dia=%s AND saida IS NOT NULL ORDER BY saida DESC LIMIT 1",
            (fid, hoje()),
        )
        fechado = cur.fetchone()
    return render_template("pessoa.html", f=f, aberto=aberto, fechado=fechado)


@app.route("/ponto/<int:fid>/entrada", methods=["POST"])
def bater_entrada(fid):
    with db() as conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("SELECT * FROM funcionarios WHERE id=%s AND ativo", (fid,))
        f = cur.fetchone()
        if not f:
            return redirect(url_for("ponto"))
        if registro_aberto(cur, fid):
            return redirect(url_for("pessoa", fid=fid))
        n = agora()
        cur.execute(
            "INSERT INTO pontos (funcionario_id, dia, entrada) VALUES (%s,%s,%s)",
            (fid, n.date(), n),
        )
    return render_template("ok.html", f=f, tipo="Entrada", cor="entrada",
                           hora=n.strftime("%H:%M"),
                           detalhe="Bom trabalho! Na saída a gente pergunta o almoço.")


@app.route("/ponto/<int:fid>/saida", methods=["GET", "POST"])
def bater_saida(fid):
    with db() as conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("SELECT * FROM funcionarios WHERE id=%s AND ativo", (fid,))
        f = cur.fetchone()
        if not f:
            return redirect(url_for("ponto"))
        aberto = registro_aberto(cur, fid)
        if not aberto:
            return redirect(url_for("pessoa", fid=fid))

        n = agora()
        bruto = int((n - aberto["entrada"]).total_seconds() // 60)

        if request.method == "GET":
            return render_template("almoco.html", f=f, aberto=aberto,
                                   padrao=ALMOCO_PADRAO, bruto=bruto,
                                   saida_prevista=n.strftime("%H:%M"))

        try:
            almoco = max(0, min(240, int(request.form.get("almoco") or 0)))
        except ValueError:
            almoco = ALMOCO_PADRAO

        almoco, minutos, ajustado = calcula(aberto["entrada"], n, almoco)
        cur.execute(
            "UPDATE pontos SET saida=%s, almoco_min=%s, minutos=%s WHERE id=%s",
            (n, almoco, minutos, aberto["id"]),
        )

    detalhe = f"{almoco} min de almoço registrados. Total do dia: {hm(minutos)}."
    aviso = ("A jornada passou de 7h, então o almoço foi ajustado para 30 min."
             if ajustado else None)
    return render_template("ok.html", f=f, tipo="Saída", cor="saida",
                           hora=n.strftime("%H:%M"), detalhe=detalhe, aviso=aviso)


# ---------------- Admin ----------------

def admin_only(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not session.get("admin"):
            return redirect(url_for("login"))
        return fn(*a, **kw)
    return wrapper


@app.route("/admin/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("senha") == ADMIN_SENHA:
            session["admin"] = True
            return redirect(url_for("admin"))
        return render_template("login.html", erro="Senha incorreta.")
    return render_template("login.html")


@app.route("/admin/sair")
def logout():
    session.clear()
    return redirect(url_for("ponto"))


def pendencias(cur):
    cur.execute("""
        SELECT p.id, p.dia, f.nome
        FROM pontos p JOIN funcionarios f ON f.id = p.funcionario_id
        WHERE p.saida IS NULL AND p.dia < %s
        ORDER BY p.dia
    """, (hoje(),))
    abertos = cur.fetchall()

    cur.execute("""
        SELECT f.nome FROM funcionarios f
        WHERE f.ativo AND NOT EXISTS (
            SELECT 1 FROM pontos p WHERE p.funcionario_id = f.id AND p.dia = %s
        ) ORDER BY f.nome
    """, (hoje(),))
    sem_entrada = [r["nome"] for r in cur.fetchall()]
    return abertos, sem_entrada


@app.route("/admin")
@admin_only
def admin():
    ini, fim = semana_de(hoje())
    with db() as conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("SELECT * FROM funcionarios ORDER BY ativo DESC, nome")
        equipe = cur.fetchall()
        abertos, sem_entrada = pendencias(cur)
    return render_template("admin.html", equipe=equipe, ini=ini, fim=fim,
                           abertos=abertos, sem_entrada=sem_entrada,
                           dia_semana=hoje().weekday())


@app.route("/admin/funcionario", methods=["POST"])
@admin_only
def salvar_funcionario():
    fid = request.form.get("id")
    nome = (request.form.get("nome") or "").strip()
    cargo = (request.form.get("cargo") or "").strip()
    valor = (request.form.get("valor_hora") or "0").replace(".", "").replace(",", ".")
    ativo = bool(request.form.get("ativo"))
    foto = request.form.get("foto") or None

    with db() as conn, conn.cursor() as cur:
        if fid:
            if foto:
                cur.execute("UPDATE funcionarios SET nome=%s,cargo=%s,valor_hora=%s,ativo=%s,foto=%s WHERE id=%s",
                            (nome, cargo, valor, ativo, foto, fid))
            else:
                cur.execute("UPDATE funcionarios SET nome=%s,cargo=%s,valor_hora=%s,ativo=%s WHERE id=%s",
                            (nome, cargo, valor, ativo, fid))
        else:
            cur.execute("INSERT INTO funcionarios (nome,cargo,valor_hora,foto) VALUES (%s,%s,%s,%s)",
                        (nome, cargo, valor, foto))
    return redirect(url_for("admin"))


@app.route("/admin/funcionario/<int:fid>/excluir", methods=["POST"])
@admin_only
def excluir_funcionario(fid):
    with db() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM funcionarios WHERE id=%s", (fid,))
    return redirect(url_for("admin"))


def semana_do_funcionario(cur, fid, ini, fim):
    cur.execute("""
        SELECT * FROM pontos WHERE funcionario_id=%s AND dia BETWEEN %s AND %s
        ORDER BY dia, entrada
    """, (fid, ini, fim))
    regs = {}
    for r in cur.fetchall():
        regs.setdefault(r["dia"], []).append(r)

    linhas, total = [], 0
    for i in range(7):
        d = ini + timedelta(days=i)
        for r in regs.get(d, [None]):
            total += (r["minutos"] or 0) if r else 0
            linhas.append({"dia": DIAS[i], "data": d, "reg": r})
    return linhas, total


@app.route("/admin/semana")
@admin_only
def semana():
    ini = date.fromisoformat(request.args.get("ini") or semana_de(hoje())[0].isoformat())
    fim = ini + timedelta(days=6)
    fid = request.args.get("f")

    with db() as conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        if fid:
            cur.execute("SELECT * FROM funcionarios WHERE id=%s", (fid,))
            equipe = [r for r in cur.fetchall()]
        else:
            cur.execute("SELECT * FROM funcionarios WHERE ativo ORDER BY nome")
            equipe = cur.fetchall()

        blocos = []
        for f in equipe:
            linhas, total = semana_do_funcionario(cur, f["id"], ini, fim)
            blocos.append({
                "f": f, "linhas": linhas, "total": total,
                "valor": (total / 60) * float(f["valor_hora"]),
            })

    return render_template("semana.html", blocos=blocos, ini=ini, fim=fim,
                           anterior=ini - timedelta(days=7), proxima=ini + timedelta(days=7),
                           filtro=fid, total_geral=sum(b["valor"] for b in blocos))


@app.route("/admin/semana/fechar", methods=["POST"])
@admin_only
def fechar_semana():
    ini = date.fromisoformat(request.form["ini"])
    fim = ini + timedelta(days=6)
    with db() as conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("SELECT * FROM funcionarios WHERE ativo ORDER BY nome")
        for f in cur.fetchall():
            _, total = semana_do_funcionario(cur, f["id"], ini, fim)
            valor = round((total / 60) * float(f["valor_hora"]), 2)
            cur.execute("""
                INSERT INTO fechamentos (funcionario_id, ini, fim, minutos, valor)
                VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (funcionario_id, ini, fim)
                DO UPDATE SET minutos=EXCLUDED.minutos, valor=EXCLUDED.valor, criado_em=NOW()
            """, (f["id"], ini, fim, total, valor))
    return redirect(url_for("semana", ini=ini.isoformat()))


@app.route("/admin/ficha/<int:fid>")
@admin_only
def ficha(fid):
    with db() as conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("SELECT * FROM funcionarios WHERE id=%s", (fid,))
        f = cur.fetchone()
        if not f:
            return redirect(url_for("admin"))
        cur.execute("SELECT * FROM fechamentos WHERE funcionario_id=%s ORDER BY ini DESC", (fid,))
        semanas = cur.fetchall()
    return render_template("ficha.html", f=f, semanas=semanas,
                           total=sum(float(s["valor"]) for s in semanas))


# --------- Edição manual de registros ---------

@app.route("/admin/registro", methods=["POST"])
@admin_only
def salvar_registro():
    rid = request.form.get("id")
    fid = request.form.get("funcionario_id")
    dia = date.fromisoformat(request.form["dia"])
    e = (request.form.get("entrada") or "").strip()
    s = (request.form.get("saida") or "").strip()
    try:
        almoco = max(0, min(240, int(request.form.get("almoco") or 0)))
    except ValueError:
        almoco = 0

    entrada = datetime.combine(dia, datetime.strptime(e, "%H:%M").time()) if e else None
    saida = datetime.combine(dia, datetime.strptime(s, "%H:%M").time()) if s else None
    if entrada and saida and saida < entrada:
        saida += timedelta(days=1)
    minutos = max(0, int((saida - entrada).total_seconds() // 60) - almoco) if entrada and saida else None

    with db() as conn, conn.cursor() as cur:
        if rid:
            cur.execute("UPDATE pontos SET dia=%s,entrada=%s,saida=%s,almoco_min=%s,minutos=%s WHERE id=%s",
                        (dia, entrada, saida, almoco, minutos, rid))
        else:
            cur.execute("INSERT INTO pontos (funcionario_id,dia,entrada,saida,almoco_min,minutos) VALUES (%s,%s,%s,%s,%s,%s)",
                        (fid, dia, entrada, saida, almoco, minutos))
    return redirect(request.form.get("voltar") or url_for("admin"))


@app.route("/admin/registro/<int:rid>/excluir", methods=["POST"])
@admin_only
def excluir_registro(rid):
    with db() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM pontos WHERE id=%s", (rid,))
    return redirect(request.form.get("voltar") or url_for("admin"))


# ---------------- Alertas no WhatsApp ----------------

def whats(texto):
    if not (WHATS_PHONE and WHATS_APIKEY):
        return "sem config"
    url = ("https://api.callmebot.com/whatsapp.php?"
           + urllib.parse.urlencode({"phone": WHATS_PHONE, "text": texto, "apikey": WHATS_APIKEY}))
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            r.read()
        return "ok"
    except Exception as e:
        return f"erro: {e}"


@app.route("/cron/alertas")
def alertas():
    if not CRON_TOKEN or request.args.get("token") != CRON_TOKEN:
        return "nao autorizado", 403
    tipo = request.args.get("tipo", "saida")

    with db() as conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        abertos, sem_entrada = pendencias(cur)
        if tipo == "entrada":
            cur.execute("""
                SELECT f.nome FROM funcionarios f
                WHERE f.ativo AND NOT EXISTS (
                    SELECT 1 FROM pontos p WHERE p.funcionario_id=f.id AND p.dia=%s
                ) ORDER BY f.nome
            """, (hoje(),))
            nomes = [r["nome"].split()[0] for r in cur.fetchall()]
            if not nomes or hoje().weekday() >= 5:
                return "nada a avisar"
            return whats("Ponto InkSugar: ainda sem entrada hoje — " + ", ".join(nomes))

        cur.execute("""
            SELECT f.nome, p.dia FROM pontos p JOIN funcionarios f ON f.id=p.funcionario_id
            WHERE p.saida IS NULL ORDER BY p.dia
        """)
        pend = [f"{r['nome'].split()[0]} ({r['dia'].strftime('%d/%m')})" for r in cur.fetchall()]
    if not pend:
        return "nada a avisar"
    return whats("Ponto InkSugar: saída não registrada — " + ", ".join(pend))


init_db()

if __name__ == "__main__":
    app.run(debug=True, port=5000)
