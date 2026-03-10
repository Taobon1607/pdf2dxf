# pdf2dxf.io — Deploy Guide

## Cấu trúc dự án

```
pdf-to-dxf/
├── index.html              ← Frontend (deploy Vercel hoặc Railway static)
└── backend/
    ├── main.py             ← FastAPI web server
    ├── worker.py           ← Celery conversion engine
    ├── database.py         ← SQLite usage tracking
    ├── requirements.txt
    └── Dockerfile
```

---

## Deploy Railway (nhanh nhất)

### Bước 1 — Chuẩn bị GitHub repo
```bash
git init
git add .
git commit -m "initial"
git remote add origin https://github.com/YOUR_USERNAME/pdf2dxf
git push -u origin main
```

### Bước 2 — Railway setup
1. Vào https://railway.app → New Project → Deploy from GitHub
2. Chọn repo vừa push

### Bước 3 — Tạo Redis service
- Railway dashboard → Add Service → Redis
- Copy `REDIS_URL` từ Variables tab

### Bước 4 — Environment variables (Web service)
```
REDIS_URL=redis://...         ← từ Redis service
STRIPE_SECRET_KEY=sk_live_... ← từ Stripe dashboard
STRIPE_WEBHOOK_SECRET=whsec_...
FRONTEND_URL=https://your-frontend.com
```

### Bước 5 — Tạo Celery Worker service
- Railway → Add Service → GitHub repo (cùng repo)
- Override START COMMAND: `celery -A worker worker --loglevel=info --concurrency=2`
- Add same env vars

### Bước 6 — Deploy Frontend
- Vercel → New Project → upload `index.html`
- Hoặc Railway static service

### Bước 7 — Update API_BASE trong index.html
```javascript
const API_BASE = 'https://your-railway-app.up.railway.app';
```

---

## Test local

```bash
# Terminal 1 — Redis
docker run -p 6379:6379 redis:alpine

# Terminal 2 — FastAPI
cd backend
pip install -r requirements.txt
uvicorn main:app --reload --port 8000

# Terminal 3 — Celery worker
cd backend
celery -A worker worker --loglevel=info --concurrency=2

# Open index.html in browser
```

---

## Chi phí Railway (ước tính)

| Giai đoạn | Services | Chi phí/tháng |
|---|---|---|
| Test | Web + Worker + Redis | ~$0 (free credit) |
| 1,000 users | Web + Worker + Redis | ~$21 |
| 10,000 users | Web + 2 Workers + Redis | ~$50 |

---

## Stripe setup

1. Tạo account https://stripe.com
2. Dashboard → API Keys → copy `Secret key`
3. Dashboard → Webhooks → Add endpoint:
   - URL: `https://your-app.railway.app/webhook/stripe`
   - Events: `customer.subscription.deleted`
4. Copy Signing secret → `STRIPE_WEBHOOK_SECRET`

---

## AdSense setup

Thay đoạn này trong index.html:
```html
<!-- Tìm div id="adSlot" và thay bằng: -->
<ins class="adsbygoogle"
     style="display:block"
     data-ad-client="ca-pub-XXXXXXXXXXXXXXXX"
     data-ad-slot="XXXXXXXXXX"
     data-ad-format="auto"
     data-full-width-responsive="true">
</ins>
<script>(adsbygoogle = window.adsbygoogle || []).push({});</script>
```

Cần AdSense approval trước (~1-2 tuần, cần site có traffic thật).

---

## SEO checklist

- [ ] Submit sitemap.xml lên Google Search Console
- [ ] Target keywords: "pdf to dxf online free", "convert pdf to dxf autocad"
- [ ] Đăng lên Reddit: r/AutoCAD, r/engineering, r/cad
- [ ] ProductHunt launch sau khi có 10+ users test
