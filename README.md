# Ponto InkSugar

Flask + Postgres. Roda inteiro em plano gratuito.

## Como funciona

**Para a equipe** — `/ponto`
Grade com a foto de cada uma. Toca na foto → aparece o relógio ao vivo e um botão só:
**Registrar entrada** (se o turno está fechado) ou **Registrar saída** (se está aberto).
Na saída o sistema pergunta o almoço: começa em 30 min, ajusta de 15 em 15 com − e +.
Se a jornada der 7h ou mais, o mínimo é 30 min — o sistema avisa e ajusta sozinho.
Confirma na tela: "Saída registrada · 17:12 · 30 min de almoço · total 8h12".

**Para você** — `/admin`
- Alertas no topo: quem não bateu saída e quem ainda não bateu entrada hoje.
- Cadastro: nome, cargo, valor da hora, foto — incluir, alterar, excluir.
- **Registros da semana** (seg→dom): tudo editável na mão, com total de horas e valor a receber por pessoa. Botão de imprimir com layout limpo.
- **Fechar semana**: guarda semana / total de horas / valor na ficha de cada uma.
- Ficha individual: histórico de semanas fechadas e acumulado.

## Instalação

### 1. Banco — Neon (grátis)
neon.tech → criar conta → **Create project**, região São Paulo → copiar a **Connection string**.

### 2. Deploy — Render (grátis)
Subir esta pasta no GitHub → **New → Web Service** → apontar pro repositório.
Build: `pip install -r requirements.txt` · Start: `gunicorn app:app`

Variáveis de ambiente:

| Chave | Valor |
|---|---|
| `DATABASE_URL` | connection string do Neon |
| `SECRET_KEY` | qualquer texto aleatório longo |
| `ADMIN_SENHA` | sua senha da área de gestão |

### 3. Avisos no WhatsApp (opcional, grátis)

**CallMeBot:** salve o número **+34 644 51 95 23** e mande `I allow callmebot to send me messages`. Ele devolve sua apikey.

Adicione no Render:

| Chave | Valor |
|---|---|
| `WHATS_PHONE` | seu número com país, ex. `5524999999999` |
| `WHATS_APIKEY` | a chave que o CallMeBot mandou |
| `CRON_TOKEN` | invente uma senha, ex. `ink2026xyz` |

**cron-job.org:** criar conta e dois jobs:
- 9h30, seg a sex → `https://SEU-APP.onrender.com/cron/alertas?token=SEU_TOKEN&tipo=entrada`
- 19h00, todo dia → `https://SEU-APP.onrender.com/cron/alertas?token=SEU_TOKEN&tipo=saida`

O aviso chega na varredura, não na hora exata. Se não tiver ninguém pendente, não manda nada.

## Rodar na sua máquina
```
pip install -r requirements.txt
export DATABASE_URL="postgresql://..."
python app.py
```

**Atenção:** no free do Render o serviço hiberna. O primeiro acesso do dia leva ~40s pra abrir — avise a equipe pra não achar que travou. O cron das 9h30 já serve de despertador.
