import base64
import os
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime, date, timedelta
from functools import wraps
from zoneinfo import ZoneInfo

import psycopg2
import psycopg2.extras
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, Response

TZ = ZoneInfo("America/Sao_Paulo")
DIAS = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]
ALMOCO_PADRAO = 30
JORNADA_LIMITE = 420  # 7h em minutos

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "troque-isso")
app.permanent_session_lifetime = timedelta(days=30)

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
                hibrido BOOLEAN NOT NULL DEFAULT FALSE,
                valor_hora_home NUMERIC(10,2) NOT NULL DEFAULT 0,
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
                minutos INTEGER,
                local TEXT NOT NULL DEFAULT 'interno'
            );
            CREATE TABLE IF NOT EXISTS fechamentos (
                id SERIAL PRIMARY KEY,
                funcionario_id INTEGER NOT NULL REFERENCES funcionarios(id) ON DELETE CASCADE,
                ini DATE NOT NULL,
                fim DATE NOT NULL,
                minutos INTEGER NOT NULL,
                valor NUMERIC(10,2) NOT NULL,
                bonus NUMERIC(10,2) NOT NULL DEFAULT 0,
                desconto NUMERIC(10,2) NOT NULL DEFAULT 0,
                obs TEXT DEFAULT '',
                criado_em TIMESTAMP NOT NULL DEFAULT NOW(),
                UNIQUE (funcionario_id, ini, fim)
            );
            CREATE TABLE IF NOT EXISTS ajustes (
                id SERIAL PRIMARY KEY,
                funcionario_id INTEGER NOT NULL REFERENCES funcionarios(id) ON DELETE CASCADE,
                ini DATE NOT NULL,
                bonus NUMERIC(10,2) NOT NULL DEFAULT 0,
                desconto NUMERIC(10,2) NOT NULL DEFAULT 0,
                obs TEXT DEFAULT '',
                UNIQUE (funcionario_id, ini)
            );
            CREATE INDEX IF NOT EXISTS idx_pontos_dia ON pontos (dia);
            ALTER TABLE funcionarios ADD COLUMN IF NOT EXISTS hibrido BOOLEAN NOT NULL DEFAULT FALSE;
            ALTER TABLE funcionarios ADD COLUMN IF NOT EXISTS valor_hora_home NUMERIC(10,2) NOT NULL DEFAULT 0;
            ALTER TABLE pontos ADD COLUMN IF NOT EXISTS local TEXT NOT NULL DEFAULT 'interno';
            ALTER TABLE fechamentos ADD COLUMN IF NOT EXISTS bonus NUMERIC(10,2) NOT NULL DEFAULT 0;
            ALTER TABLE fechamentos ADD COLUMN IF NOT EXISTS desconto NUMERIC(10,2) NOT NULL DEFAULT 0;
            ALTER TABLE fechamentos ADD COLUMN IF NOT EXISTS obs TEXT DEFAULT '';
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


def dinheiro(txt):
    txt = (txt or "0").strip().replace("R$", "").replace(" ", "")
    if "," in txt:
        txt = txt.replace(".", "").replace(",", ".")
    try:
        return round(float(txt), 2)
    except ValueError:
        return 0.0


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


@app.route("/sw.js")
def sw():
    return app.send_static_file("sw.js"), 200, {"Content-Type": "application/javascript"}


@app.route("/manifest.json")
def manifest():
    return app.send_static_file("manifest.json"), 200, {"Content-Type": "application/manifest+json"}


@app.route("/manifest/<int:fid>.json")
def manifest_pessoa(fid):
    with db() as conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("SELECT * FROM funcionarios WHERE id=%s", (fid,))
        f = cur.fetchone()
    if not f:
        return redirect(url_for("manifest"))

    icone = url_for("icone_pessoa", fid=fid) if f["foto"] else "/static/icon-512.png"
    dados = {
        "name": f"Ponto — {f['nome']}",
        "short_name": f["nome"].split()[0],
        "start_url": f"/ponto/{fid}",
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#0A0A0B",
        "theme_color": "#0A0A0B",
        "lang": "pt-BR",
        "icons": [
            {"src": icone, "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": icone, "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    }
    return jsonify(dados), 200, {"Content-Type": "application/manifest+json"}


@app.route("/icone/<int:fid>.png")
def icone_pessoa(fid):
    with db() as conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("SELECT foto FROM funcionarios WHERE id=%s", (fid,))
        f = cur.fetchone()
    if not f or not f["foto"] or "," not in f["foto"]:
        return redirect("/static/icon-512.png")
    try:
        bruto = base64.b64decode(f["foto"].split(",", 1)[1])
    except Exception:
        return redirect("/static/icon-512.png")
    return Response(bruto, mimetype="image/jpeg",
                    headers={"Cache-Control": "public, max-age=86400"})


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
    return render_template("pessoa.html", f=f, aberto=aberto, fechado=fechado,
                           feito=request.args.get("feito"),
                           ajustado=request.args.get("ajustado"))


@app.route("/ponto/<int:fid>/semana")
def semana_pessoa(fid):
    ini, fim = semana_de(hoje())
    with db() as conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("SELECT * FROM funcionarios WHERE id=%s AND ativo", (fid,))
        f = cur.fetchone()
        if not f:
            return redirect(url_for("ponto"))
        linhas, interno, home = semana_do_funcionario(cur, fid, ini, fim)
    return render_template("minha_semana.html", f=f, linhas=linhas, ini=ini, fim=fim,
                           total=interno + home, interno=interno, home=home)


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
    return redirect(url_for("pessoa", fid=fid, feito="entrada"))


@app.route("/ponto/<int:fid>/saida", methods=["POST"])
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

        try:
            almoco = max(0, min(240, int(request.form.get("almoco") or 0)))
        except ValueError:
            almoco = ALMOCO_PADRAO
        local = "home" if (f["hibrido"] and request.form.get("home")) else "interno"

        almoco, minutos, ajustado = calcula(aberto["entrada"], n, almoco)
        cur.execute(
            "UPDATE pontos SET saida=%s, almoco_min=%s, minutos=%s, local=%s WHERE id=%s",
            (n, almoco, minutos, local, aberto["id"]),
        )

    return redirect(url_for("pessoa", fid=fid, feito="saida",
                            ajustado=1 if ajustado else None))


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
            session.permanent = True
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
    valor = dinheiro(request.form.get("valor_hora"))
    hibrido = bool(request.form.get("hibrido"))
    valor_home = dinheiro(request.form.get("valor_hora_home")) if hibrido else 0
    ativo = bool(request.form.get("ativo"))
    foto = request.form.get("foto") or None

    with db() as conn, conn.cursor() as cur:
        if fid:
            if foto:
                cur.execute("""UPDATE funcionarios SET nome=%s,cargo=%s,valor_hora=%s,
                               hibrido=%s,valor_hora_home=%s,ativo=%s,foto=%s WHERE id=%s""",
                            (nome, cargo, valor, hibrido, valor_home, ativo, foto, fid))
            else:
                cur.execute("""UPDATE funcionarios SET nome=%s,cargo=%s,valor_hora=%s,
                               hibrido=%s,valor_hora_home=%s,ativo=%s WHERE id=%s""",
                            (nome, cargo, valor, hibrido, valor_home, ativo, fid))
        else:
            cur.execute("""INSERT INTO funcionarios (nome,cargo,valor_hora,hibrido,valor_hora_home,foto)
                           VALUES (%s,%s,%s,%s,%s,%s)""",
                        (nome, cargo, valor, hibrido, valor_home, foto))
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

    linhas, interno, home = [], 0, 0
    for i in range(7):
        d = ini + timedelta(days=i)
        for r in regs.get(d, [None]):
            if r and r["minutos"]:
                if r["local"] == "home":
                    home += r["minutos"]
                else:
                    interno += r["minutos"]
            linhas.append({"dia": DIAS[i], "data": d, "reg": r})
    return linhas, interno, home


def ajuste_da_semana(cur, fid, ini):
    cur.execute("SELECT * FROM ajustes WHERE funcionario_id=%s AND ini=%s", (fid, ini))
    a = cur.fetchone()
    return {
        "bonus": float(a["bonus"]) if a else 0.0,
        "desconto": float(a["desconto"]) if a else 0.0,
        "obs": (a["obs"] if a else "") or "",
    }


def bloco_semana(cur, f, ini, fim):
    linhas, interno, home = semana_do_funcionario(cur, f["id"], ini, fim)
    aj = ajuste_da_semana(cur, f["id"], ini)
    bruto = (interno / 60) * float(f["valor_hora"]) + (home / 60) * float(f["valor_hora_home"])
    return {
        "f": f, "linhas": linhas,
        "interno": interno, "home": home, "total": interno + home,
        "bruto": round(bruto, 2), "bonus": aj["bonus"], "desconto": aj["desconto"], "obs": aj["obs"],
        "valor": round(bruto + aj["bonus"] - aj["desconto"], 2),
    }


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

        blocos = [bloco_semana(cur, f, ini, fim) for f in equipe]
        cur.execute("SELECT * FROM fechamentos WHERE ini=%s ORDER BY criado_em DESC LIMIT 1", (ini,))
        ja = cur.fetchone()

    return render_template("semana.html", blocos=blocos, ini=ini, fim=fim,
                           anterior=ini - timedelta(days=7), proxima=ini + timedelta(days=7),
                           filtro=fid, total_geral=sum(b["valor"] for b in blocos),
                           fechada=ja["criado_em"] if ja else None,
                           confirmar=request.args.get("confirmar"))


@app.route("/admin/semana/ajuste", methods=["POST"])
@admin_only
def salvar_ajuste():
    fid = request.form["funcionario_id"]
    ini = date.fromisoformat(request.form["ini"])
    bonus = dinheiro(request.form.get("bonus"))
    desconto = dinheiro(request.form.get("desconto"))
    obs = (request.form.get("obs") or "").strip()[:200]
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO ajustes (funcionario_id, ini, bonus, desconto, obs)
            VALUES (%s,%s,%s,%s,%s)
            ON CONFLICT (funcionario_id, ini)
            DO UPDATE SET bonus=EXCLUDED.bonus, desconto=EXCLUDED.desconto, obs=EXCLUDED.obs
        """, (fid, ini, bonus, desconto, obs))
    return redirect(request.form.get("voltar") or url_for("semana", ini=ini.isoformat()))


@app.route("/admin/semana/fechar", methods=["POST"])
@admin_only
def fechar_semana():
    ini = date.fromisoformat(request.form["ini"])
    fim = ini + timedelta(days=6)
    confirmado = request.form.get("confirmado")

    with db() as conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        if not confirmado:
            cur.execute("SELECT 1 FROM fechamentos WHERE ini=%s LIMIT 1", (ini,))
            if cur.fetchone():
                return redirect(url_for("semana", ini=ini.isoformat(), confirmar=1))

        cur.execute("SELECT * FROM funcionarios WHERE ativo ORDER BY nome")
        for f in cur.fetchall():
            b = bloco_semana(cur, f, ini, fim)
            cur.execute("""
                INSERT INTO fechamentos (funcionario_id, ini, fim, minutos, valor, bonus, desconto, obs)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (funcionario_id, ini, fim)
                DO UPDATE SET minutos=EXCLUDED.minutos, valor=EXCLUDED.valor,
                              bonus=EXCLUDED.bonus, desconto=EXCLUDED.desconto,
                              obs=EXCLUDED.obs, criado_em=NOW()
            """, (f["id"], ini, fim, b["total"], b["valor"], b["bonus"], b["desconto"], b["obs"]))
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

        if tipo == "fechamento":
            ini, fim = semana_de(hoje() - timedelta(days=1))
            cur.execute("SELECT 1 FROM fechamentos WHERE ini=%s LIMIT 1", (ini,))
            if cur.fetchone():
                return "semana ja fechada"
            return whats(f"Ponto InkSugar: semana {ini.strftime('%d/%m')} a {fim.strftime('%d/%m')} "
                         f"pronta pra fechar quando você puder.")

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
