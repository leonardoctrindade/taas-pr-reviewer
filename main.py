from fastapi import FastAPI, Request, HTTPException
import logging
import os
import base64
import httpx

app = FastAPI()

ADO_PAT = os.environ["ADO_PAT"]
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]
ADO_ORG = os.environ["ADO_ORG"]
ADO_PROJECT = os.environ["ADO_PROJECT"]

B64_PAT = base64.b64encode(f":{ADO_PAT}".encode()).decode()
ADO_HEADERS = {
    "Authorization": f"Basic {B64_PAT}",
    "Content-Type": "application/json",
}
BASE_URL = f"https://dev.azure.com/{ADO_ORG}/{ADO_PROJECT}/_apis"

logging.basicConfig(level=logging.INFO)


@app.post("/review-pr")
async def review_pr(request: Request):
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event_type = payload.get("eventType", "")
    if event_type != "git.pullrequest.created":
        return {"status": "ignored"}

    resource = payload.get("resource", {})
    pr_id = resource.get("pullRequestId")
    repo_id = resource.get("repository", {}).get("id")
    pr_title = resource.get("title", "")
    author = resource.get("createdBy", {}).get("displayName", "")

    if not pr_id or not repo_id:
        raise HTTPException(status_code=400, detail="Missing PR data")

    logging.info(f"Novo PR #{pr_id}: {pr_title} — {author}")

    diff_text = get_pr_diff(repo_id, pr_id)
    feedback = analyze_with_openrouter(pr_title, author, diff_text)
    post_comment(repo_id, pr_id, feedback)

    return {"status": "ok"}


def get_pr_diff(repo_id: str, pr_id: int) -> str:
    with httpx.Client() as client:
        r = client.get(
            f"{BASE_URL}/git/repositories/{repo_id}/pullRequests/{pr_id}/iterations?api-version=7.1",
            headers=ADO_HEADERS,
        )
        r.raise_for_status()
        iterations = r.json().get("value", [])
        if not iterations:
            return "Nenhuma iteração encontrada."
        latest_iter = iterations[-1]["id"]

        r = client.get(
            f"{BASE_URL}/git/repositories/{repo_id}/pullRequests/{pr_id}/iterations/{latest_iter}/changes?api-version=7.1",
            headers=ADO_HEADERS,
        )
        r.raise_for_status()
        changes = r.json().get("changeEntries", [])

    diff_parts = []
    with httpx.Client() as client:
        for change in changes[:20]:
            item = change.get("item", {})
            path = item.get("path", "")
            change_type = change.get("changeType", "")

            if change_type == "delete":
                diff_parts.append(f"### {path} (deletado)")
                continue

            object_id = item.get("objectId")
            if not object_id:
                continue

            try:
                r = client.get(
                    f"{BASE_URL}/git/repositories/{repo_id}/blobs/{object_id}?api-version=7.1",
                    headers={**ADO_HEADERS, "Accept": "text/plain"},
                )
                content = r.text[:3000]
                diff_parts.append(f"### {path} ({change_type})\n```\n{content}\n```")
            except Exception:
                diff_parts.append(f"### {path} ({change_type}) — conteúdo não disponível")

    return "\n\n".join(diff_parts) if diff_parts else "Nenhum arquivo encontrado no diff."


def analyze_with_openrouter(pr_title: str, author: str, diff_text: str) -> str:
    with httpx.Client() as client:
        r = client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "openai/gpt-4.1-mini",
                "max_tokens": 2048,
                "messages": [
                    {
                        "role": "user",
                        "content": f"""Você é um revisor de código. Analise SOMENTE o código novo introduzido neste PR — ignore o código legado já existente no sistema.

**PR:** {pr_title}
**Autor:** {author}

**Código introduzido no PR:**
{diff_text}

Regras da revisão:
- Analise APENAS o que foi adicionado ou modificado neste PR, não julgue o sistema legado
- NÃO avalie clean code, padrões de projeto, nomenclatura ou organização de código
- Foque exclusivamente nos itens abaixo:

1. **Código malicioso** — backdoors, exfiltração de dados, execução de comandos externos não autorizados, acesso indevido a recursos
2. **Loops infinitos ou problemáticos** — while sem condição de saída, recursão sem limite, loops que podem travar o sistema
3. **Logs e prints indevidos** — console.log, print(), Debug.WriteLine ou similares logando objetos completos, arrays, listas ou dados sensíveis em produção
4. **Falhas de segurança graves** — SQL injection, credenciais hardcoded, senhas ou tokens expostos no código
5. **Erros críticos de lógica** — divisão por zero, null reference não tratado, condições que sempre retornam true/false

Ao final, emita um veredicto obrigatório:
- ✅ APROVADO — se não encontrou nenhum dos problemas acima
- ❌ REPROVADO — se encontrou qualquer um dos problemas acima, listando cada ocorrência com o arquivo e linha

Seja direto. Não faça sugestões de melhoria, apenas aponte problemas críticos.""",
                    }
                ],
            },
            timeout=60,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


def post_comment(repo_id: str, pr_id: int, feedback: str) -> None:
    body = {
        "comments": [
            {
                "parentCommentId": 0,
                "content": f"## 🤖 Revisão automática de código\n\n{feedback}\n\n---\n*Revisão gerada automaticamente — analisa apenas o código introduzido neste PR.*",
                "commentType": 1,
            }
        ],
        "status": 1,
    }
    with httpx.Client() as client:
        r = client.post(
            f"{BASE_URL}/git/repositories/{repo_id}/pullRequests/{pr_id}/threads?api-version=7.1",
            headers=ADO_HEADERS,
            json=body,
        )
        r.raise_for_status()
    logging.info(f"Comentário postado no PR #{pr_id}")
