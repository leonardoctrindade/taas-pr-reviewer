import os, base64, json, urllib.request

PAT      = os.environ["ADO_PAT"]
OR_KEY   = os.environ["OPENROUTER_API_KEY"]
PR_ID    = os.environ["PR_ID"]
REPO_ID  = os.environ["REPO_ID"]
PR_TITLE = os.environ.get("PR_TITLE", "")
AUTHOR   = os.environ.get("AUTHOR", "")
ORG      = os.environ["ORG"]
PROJECT  = os.environ["PROJECT"]

B64 = base64.b64encode(f":{PAT}".encode()).decode()
HDR = {"Authorization": f"Basic {B64}", "Content-Type": "application/json"}
BASE = f"https://dev.azure.com/{ORG}/{PROJECT}/_apis"

def ado_get(url):
    req = urllib.request.Request(url, headers=HDR)
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())

def ado_post(url, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=HDR, method="POST")
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())

iters = ado_get(f"{BASE}/git/repositories/{REPO_ID}/pullRequests/{PR_ID}/iterations?api-version=7.1")
latest = iters["value"][-1]["id"]
changes = ado_get(f"{BASE}/git/repositories/{REPO_ID}/pullRequests/{PR_ID}/iterations/{latest}/changes?api-version=7.1")

diff_parts = []
for change in changes.get("changeEntries", [])[:20]:
    item  = change.get("item", {})
    path  = item.get("path", "")
    ctype = change.get("changeType", "")
    oid   = item.get("objectId")
    if ctype == "delete" or not oid:
        diff_parts.append(f"### {path} ({ctype})")
        continue
    try:
        req = urllib.request.Request(
            f"{BASE}/git/repositories/{REPO_ID}/blobs/{oid}?api-version=7.1",
            headers={**HDR, "Accept": "text/plain"}
        )
        with urllib.request.urlopen(req) as r:
            content = r.read().decode("utf-8", errors="replace")[:3000]
        diff_parts.append(f"### {path} ({ctype})\n```\n{content}\n```")
    except Exception as e:
        diff_parts.append(f"### {path} ({ctype}) - nao disponivel: {e}")

diff_text = "\n\n".join(diff_parts) if diff_parts else "Nenhum arquivo encontrado."

prompt = f"""Voce e um revisor de codigo. Analise SOMENTE o codigo novo introduzido neste PR.

PR: {PR_TITLE}
Autor: {AUTHOR}

Codigo introduzido no PR:
{diff_text}

Regras:
- Analise APENAS o que foi adicionado ou modificado, ignore o sistema legado
- NAO avalie clean code, padroes ou nomenclatura
- Foque em:
1. Codigo malicioso - backdoors, exfiltracao de dados, execucao de comandos nao autorizados
2. Loops infinitos - while sem saida, recursao sem limite
3. Logs indevidos - print(), console.log, Debug.WriteLine logando objetos, arrays ou dados sensiveis
4. Falhas de seguranca - SQL injection, credenciais hardcoded, tokens expostos
5. Erros criticos de logica - divisao por zero, null reference nao tratado

Ao final emita obrigatoriamente:
- APROVADO - se nao encontrou nenhum problema
- REPROVADO - se encontrou problemas, listando arquivo e linha

Seja direto. Nao faca sugestoes de melhoria."""

payload = json.dumps({
    "model": "openai/gpt-4.1-mini",
    "max_tokens": 2048,
    "messages": [{"role": "user", "content": prompt}]
}).encode()

req = urllib.request.Request(
    "https://openrouter.ai/api/v1/chat/completions",
    data=payload,
    headers={"Authorization": f"Bearer {OR_KEY}", "Content-Type": "application/json"},
    method="POST"
)
with urllib.request.urlopen(req, timeout=60) as r:
    feedback = json.loads(r.read())["choices"][0]["message"]["content"]

comment_body = {
    "comments": [{
        "parentCommentId": 0,
        "content": f"## Revisao automatica de codigo\n\n{feedback}\n\n---\n*Analisa apenas o codigo introduzido neste PR.*",
        "commentType": 1
    }],
    "status": 1
}
ado_post(
    f"{BASE}/git/repositories/{REPO_ID}/pullRequests/{PR_ID}/threads?api-version=7.1",
    comment_body
)
print(f"Comentario postado no PR #{PR_ID}")
