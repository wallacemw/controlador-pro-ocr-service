# OCR Service Dedicado

Este projeto já está preparado para usar um OCR robusto externo em produção, mantendo:

- Vercel para frontend, Supabase e Ably
- serviço OCR dedicado para `PaddleOCR`

## Objetivo

A versão hospedada na Vercel continua a expor:

- `/api/health`
- `/api/receipt/parse`

Mas, quando `OCR_SERVICE_UPSTREAM_URL` estiver configurado, esses endpoints passam a fazer proxy para o OCR dedicado.

Assim, o frontend não muda de URL e o deploy principal continua simples.

## Runtime recomendado

- Python `3.11`
- `PaddleOCR` + `PaddlePaddle`
- deploy por Docker

## Arquivos de deploy

- `Dockerfile.ocr`
- `requirements-ocr-service.txt`
- `receipt_service.py`
- `render.yaml`
- `.dockerignore`

## Deploy recomendado

O caminho mais simples nesta fase é usar a [Render com web services Docker](https://render.com/docs/docker) e blueprint [`render.yaml`](https://render.com/docs/blueprint-spec).

### Passos na Render

1. Ligue este repositório ao Render.
2. Escolha `Blueprint` ou importe o `render.yaml`.
3. Confirme o serviço `controlador-pro-ocr`.
4. Configure o secret:
   - `OCR_SERVICE_SHARED_SECRET`
5. Opcionalmente configure:
   - `GEMINI_API_KEY`
6. Faça o deploy.

Depois do deploy, guarde a URL pública do OCR dedicado, por exemplo:

- `https://controlador-pro-ocr.onrender.com`

## Variáveis no OCR dedicado

Configure no serviço OCR:

- `RECEIPT_SERVICE_HOST=0.0.0.0`
- `RECEIPT_SERVICE_PORT=10000`
- `RECEIPT_OCR_LANG=pt`
- `GEMINI_API_KEY=` opcional
- `GEMINI_RECEIPT_MODEL=gemini-2.5-flash`
- `OCR_SERVICE_SHARED_SECRET=uma-chave-forte`
- `OCR_SERVICE_ENFORCE_SHARED_SECRET=1`
- `PADDLE_OCR_ENABLE_WARMUP=1`
- `PADDLE_OCR_STARTUP_GRACE_MS=8000`
- `PADDLE_OCR_USE_DOC_ORIENTATION_CLASSIFY=0`
- `PADDLE_OCR_USE_DOC_UNWARPING=0`
- `PADDLE_OCR_USE_TEXTLINE_ORIENTATION=0`
- `PADDLE_OCR_TEXT_DETECTION_MODEL_NAME=PP-OCRv5_mobile_det`

Esses defaults reduzem o cold start e aquecem o PaddleOCR antes da primeira leitura com imagem.

## Variáveis na Vercel

Configure na Vercel:

- `OCR_SERVICE_UPSTREAM_URL=https://seu-ocr-dedicado.exemplo.com`
- `OCR_SERVICE_UPSTREAM_TIMEOUT_MS=25000`
- `OCR_SERVICE_SHARED_SECRET=mesma-chave-forte`

Não ative `OCR_SERVICE_ENFORCE_SHARED_SECRET` na Vercel; ele é usado no serviço OCR dedicado.

## Comportamento esperado

- se o OCR dedicado estiver saudável, a produção passa a reportar `PaddleOCR` no `health`
- se o OCR dedicado cair, a Vercel continua a responder com fallback `parser service`
- o app continua local-first e não perde dados locais por falha remota

## Smoke test rápido

1. Abra `https://controlador-gastos-pro.vercel.app/api/health`
2. Verifique:
   - `mode`
   - `backends.paddleocr`
   - `details.message`
3. Faça uma leitura de recibo em produção
4. Confirme no painel técnico se o OCR saiu de `parser service` para `PaddleOCR`
