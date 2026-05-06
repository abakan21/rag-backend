# A.S.S. Lover — Backend (RAG Ingestion API)

> FastAPI backend pro inteligentní sběr webového obsahu a RAG vyhledávání

## Přehled

Backend slouží jako orchestrátor a ingest engine systému A.S.S. Lover. Zajišťuje:
- Multimodální sběr webového obsahu (HTML scraping, deep crawl)
- Indexování dokumentů do vektorové databáze Qdrant
- RAG vyhledávání s generováním odpovědí přes LLM
- Autentizaci a autorizaci přes Keycloak (OIDC/OAuth2)

## Architektura

```
Frontend → nginx → FastAPI (backend)
                       ├── ingestion.py   # Scraping pipeline (httpx + BeautifulSoup)
                       ├── rag.py         # RAG pipeline (LangChain + Qdrant + LLM)
                       ├── auth.py        # Keycloak JWT ověření
                       ├── models.py      # SQLAlchemy modely (PostgreSQL)
                       └── scheduler.py   # Plánované úlohy
```

## Technologie

| Komponenta | Technologie |
|---|---|
| Framework | FastAPI + Gunicorn/Uvicorn |
| Databáze | PostgreSQL (SQLAlchemy) |
| Vektorová DB | Qdrant |
| Embeddingy | HuggingFace `paraphrase-multilingual-MiniLM-L12-v2` (CPU) |
| LLM | e-infra API (`llama-4-scout-17b-16e-instruct`) |
| RAG pipeline | LangChain |
| Scraping | httpx + BeautifulSoup4 + markdownify |
| Auth | Keycloak (OIDC/OAuth2, JWT RS256) |
| Fronta | Redis (background tasks) |

## API Endpointy

### Autentizace
| Metoda | Endpoint | Popis | Role |
|---|---|---|---|
| GET | `/api/auth/me` | Profil přihlášeného uživatele | user+ |

### Ingestion
| Metoda | Endpoint | Popis | Role |
|---|---|---|---|
| POST | `/api/ingest` | Spustit sběr dat z URL | admin |
| GET | `/api/jobs` | Seznam ingestion jobů | admin |
| DELETE | `/api/jobs/{id}` | Smazat job a jeho vektory | admin |
| GET | `/api/jobs/{id}/detail` | Detail jobu + evidence artefakty | user+ |
| GET | `/api/jobs/{id}/files` | Seznam extrahovaných souborů | user+ |
| PUT | `/api/jobs/{id}/resolve` | Vyřešit incident (CAPTCHA/FAILED) | admin |

### Zdroje
| Metoda | Endpoint | Popis | Role |
|---|---|---|---|
| GET | `/api/sources` | Seznam registrovaných zdrojů | user+ |
| DELETE | `/api/sources/{id}` | Smazat zdroj | admin |

### Vyhledávání
| Metoda | Endpoint | Popis | Role |
|---|---|---|---|
| POST | `/api/search` | RAG vyhledávání + generování odpovědi | volitelné |

### Analytics & Soubory
| Metoda | Endpoint | Popis | Role |
|---|---|---|---|
| GET | `/api/analytics` | Přehled statistik systému | user+ |
| GET | `/api/files/{filename}` | Obsah extrahovaného dokumentu | volitelné |
| GET | `/api/evidence/{id}/file` | Screenshot evidence artefakt | user+ |

## Ingest Pipeline

Systém používá vícevrstvou strategii pro sběr obsahu:

```
URL → robots.txt check (bypass pro consent/contract)
    → HTTP scraping (httpx + BeautifulSoup)
    → Markdown konverze
    → Chunking (LangChain MarkdownTextSplitter)
    → Embeddingy (HuggingFace MiniLM)
    → Qdrant indexování
```

### Strategie ingestu
- **HTML** — standardní HTTP GET + parsování DOM
- **Rendered DOM** — fallback pro JS-heavy stránky (Playwright)
- **Screenshot + OCR** — fallback pro vizuálně komplexní stránky

### Deep Crawl
- BFS procházení interních odkazů
- Maximálně 30 stránek na job
- Normalizace URL (odstranění duplicit s query params)

### CAPTCHA & Incidenty
- Detekce CAPTCHA v obsahu stránky
- Ukládání evidence artefaktů (screenshoty, logy)
- Workflow: `CAPTCHA_DETECTED → resolve → retry`

### robots.txt
- Respektování robots.txt pro `public` zdroje
- Bypass pro `consent` a `contract` zdroje (smluvní souhlas s majitelem)

## Instalace a spuštění

### Požadavky
- Python 3.11+
- PostgreSQL
- Qdrant
- Redis
- Keycloak

### Lokální vývoj

```bash
pip install -r requirements.txt

# Nastavení prostředí
export DATABASE_URL="postgresql://user:pass@localhost:5432/ragdb"
export QDRANT_HOST="localhost"
export QDRANT_PORT="6333"
export OLLAMA_URL="https://llm.ai.e-infra.cz/v1"
export OLLAMA_API_KEY="váš-api-klíč"
export LLM_MODEL_NAME="llama-4-scout-17b-16e-instruct"
export KEYCLOAK_URL="http://localhost:8080"
export KEYCLOAK_PUBLIC_URL="http://localhost:8080"

# Spuštění
uvicorn main:app --reload
```

### Docker

Viz repozitář [rag-infra](https://github.com/abakan21/rag-infra).

## Proměnné prostředí

| Proměnná | Popis | Výchozí |
|---|---|---|
| `DATABASE_URL` | PostgreSQL connection string | `sqlite:///./rag_storage.db` |
| `QDRANT_HOST` | Qdrant hostname | `localhost` |
| `QDRANT_PORT` | Qdrant port | `6333` |
| `OLLAMA_URL` | LLM API base URL | `https://llm.ai.e-infra.cz/v1` |
| `OLLAMA_API_KEY` | LLM API klíč | — |
| `LLM_MODEL_NAME` | Název LLM modelu | `llama-4-scout-17b-16e-instruct` |
| `KEYCLOAK_URL` | Interní URL Keycloaku | `http://keycloak:8080` |
| `KEYCLOAK_PUBLIC_URL` | Veřejná URL Keycloaku | `http://localhost:8080` |
| `KEYCLOAK_REALM` | Název Keycloak realmu | `rag` |
| `CORS_ORIGINS` | Povolené CORS origins | `http://localhost` |
| `DATA_DIR` | Adresář pro ukládání souborů | `data` |

## Datový model

```
Source (1) ──→ (N) IngestJob (1) ──→ (N) Evidence
                                         (screenshot, markdown)
```

| Entita | Popis |
|---|---|
| `Source` | Registr webových zdrojů s konfigurací |
| `IngestJob` | Záznamy o jednotlivých úlohách sběru |
| `Evidence` | Důkazní artefakty (screenshoty, markdown soubory) |

## RBAC Role

| Role | Oprávnění |
|---|---|
| `admin` | Plný přístup (ingest, správa zdrojů, uživatelů) |
| `user` | Čtení, vyhledávání |
