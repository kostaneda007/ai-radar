import feedparser, requests, time, json, os, re, random, hashlib
from openai import OpenAI
from datetime import datetime
import config

feedparser.USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
import socket
socket.setdefaulttimeout(20)

client = OpenAI(api_key=config.LLM_API_KEY, base_url=config.LLM_BASE_URL)
PUBLISHED_FILE = "published_news.json"
IMAGES_DIR = "images"
os.makedirs(IMAGES_DIR, exist_ok=True)

def load_published():
    if os.path.exists(PUBLISHED_FILE):
        with open(PUBLISHED_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return []

def save_published(data):
    with open(PUBLISHED_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def norm_title(t):
    return re.sub(r'[^a-zа-яё0-9]+', '', (t or '').lower())

def norm_link(u):
    return (u or '').split('?')[0].rstrip('/')

def clean_russian_text(text):
    if not text:
        return text
    text = re.sub(r'[一-鿿-䶿]+', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def sanitize_post(text):
    if not text:
        return None
    if '🔥' in text and ('We need' in text or "Let's" in text or 'Draft' in text):
        text = text[text.index('🔥'):]
    latin = len(re.findall(r'[A-Za-z]', text))
    cyr = len(re.findall(r'[А-Яа-яЁё]', text))
    if latin > cyr:
        return None
    if len(text) > 1000:
        text = text[:1000].rsplit('\n', 1)[0]
    return text.strip()

def get_news():
    all_news = []
    published = set(load_published())
    seen = set()
    cutoff = time.time() - 7 * 24 * 3600
    for category, feeds in config.RSS_FEEDS.items():
        for feed_url in feeds:
            try:
                feed = feedparser.parse(feed_url)
                for entry in feed.entries[:15]:
                    news_id = entry.get('id', entry.link)
                    tkey = norm_title(entry.title)
                    lkey = norm_link(entry.link)
                    if news_id in published or tkey in published or lkey in published:
                        continue
                    if tkey in seen or lkey in seen:
                        continue
                    ts = entry.get('published_parsed') or entry.get('updated_parsed')
                    if ts:
                        ts_time = time.mktime(ts)
                        if ts_time >= cutoff:
                            seen.add(tkey)
                            seen.add(lkey)
                            all_news.append({
                                'id': news_id, 'tkey': tkey, 'lkey': lkey,
                                'category': category,
                                'title': entry.title,
                                'summary': entry.get('summary', entry.get('description', '')),
                                'link': entry.link,
                                'ts': ts_time
                            })
            except Exception as e:
                print(f"Ошибка RSS {feed_url}: {e}")
    all_news.sort(key=lambda n: n['ts'], reverse=True)
    return all_news[:8]

def rewrite(news):
    system_prompt = """Ты — русский журналист о технологиях. Пиши ПРОСТЫМ и ПОНЯТНЫМ русским языком, как живой человек.
Правила:
- Короткие предложения. Простые слова.
- Никакого канцелярита и машинного перевода.
- Заголовок — ясная суть новости за 5-8 слов, без загадок.
- Только русский язык, кириллица и эмодзи. Никаких иероглифов.
- До 800 символов."""
    user_prompt = f"""Английская новость: {news['title']}
Кратко: {news['summary']}

Перескажи её ПО-РУССКИ своими словами, просто и понятно:
🔥 Заголовок — простыми словами суть (5-8 слов)
Два абзаца: что случилось и почему это важно
💡 Инсайт: одно предложение простыми словами
👇 Вопрос читателям
3 хештега на русском"""
    for attempt in range(3):
        try:
            resp = client.chat.completions.create(
                model=config.MODEL_NAME,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.7,
                max_tokens=600
            )
            return sanitize_post(clean_russian_text(resp.choices[0].message.content))
        except Exception as e:
            print(f"⚠️ Попытка {attempt+1}/3 не удалась: {str(e)[:60]}")
            if attempt < 2:
                time.sleep(15)
    return None

def generate_image_prompt(news):
    prompt = f"Create short English prompt (2 sentences) for digital art about: {news['title']}. Modern, futuristic, bright, NO text. Only the prompt."
    try:
        resp = client.chat.completions.create(
            model=config.MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        print(f"Ошибка промпта: {e}")
        return None

def generate_image(prompt, news_id):
    try:
        safe = hashlib.sha1(news_id.encode('utf-8')).hexdigest()[:24]
        path = f"{IMAGES_DIR}/{safe}.jpg"
        encoded = requests.utils.quote(prompt)
        seed = random.randint(1, 999999)
        url_nb = f"https://image.pollinations.ai/prompt/{encoded}?width=1024&height=768&nologo=true&seed={seed}&model=nano-banana"
        url_fb = f"https://image.pollinations.ai/prompt/{encoded}?width=1024&height=768&nologo=true&seed={seed}"
        print("🎨 Генерирую картинку (nano-banana)...")
        r = requests.get(url_nb, timeout=180)
        if r.status_code != 200 or len(r.content) < 1000:
            print("🎨 nano-banana недоступен, пробую flux...")
            r = requests.get(url_fb, timeout=180)
        if r.status_code == 200 and len(r.content) > 1000:
            with open(path, 'wb') as f:
                f.write(r.content)
            print("🖼 Картинка готова")
            return path
        return None
    except Exception as e:
        print(f"Ошибка картинки: {e}")
        return None

def post_tg(text, image_path=None):
    try:
        if image_path and os.path.exists(image_path):
            caption = text[:1020] if len(text) > 1020 else text
            url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendPhoto"
            with open(image_path, 'rb') as photo:
                r = requests.post(url, files={'photo': photo},
                                  data={"chat_id": config.TELEGRAM_CHANNEL_ID, "caption": caption},
                                  timeout=60)
            if r.status_code == 200:
                return True
            print(f"TG photo error: {r.json()}")
            return post_tg(text, None)
        else:
            url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
            r = requests.post(url, data={"chat_id": config.TELEGRAM_CHANNEL_ID, "text": text}, timeout=30)
            return r.status_code == 200
    except Exception as e:
        print(f"TG exception: {e}")
        return False

def post_vk(text):
    try:
        clean = re.sub(r'<[^>]+>', '', text)
        full_text = clean + f"\n\n🚀 Больше в Telegram: {config.TELEGRAM_CHANNEL_LINK}"
        data = {
            "owner_id": f"-{config.VK_GROUP_ID}", "from_group": 1,
            "message": full_text, "access_token": config.VK_TOKEN, "v": "5.199"
        }
        r = requests.post("https://api.vk.com/method/wall.post", data=data, timeout=30)
        result = r.json()
        if 'error' in result:
            print(f"VK post error: {result['error']}")
            return False
        return 'post_id' in result.get('response', {})
    except Exception as e:
        print(f"VK exception: {e}")
        return False

def save_to_site(news, text, img):
    try:
        data = []
        if os.path.exists("news.json"):
            try:
                data = json.load(open("news.json", encoding="utf-8"))
            except Exception:
                data = []
        entry = {
            "title": news["title"],
            "text": text,
            "link": news["link"],
            "category": news["category"],
            "date": datetime.now().strftime("%d.%m.%Y %H:%M"),
            "image": os.path.basename(img) if img and os.path.exists(img) else None,
        }
        data.insert(0, entry)
        json.dump(data[:100], open("news.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print("💾 Сохранено для сайта")
    except Exception as e:
        print("Ошибка сохранения для сайта:", e)

def mark_published(published, news):
    for key in (news['id'], news['tkey'], news['lkey']):
        if key not in published:
            published.append(key)

def main():
    print(f"🚀 Запуск: {datetime.now()}")
    news_candidates = get_news()
    if not news_candidates:
        print("📭 Новых новостей нет")
        return
    print(f"📰 Кандидатов: {len(news_candidates)}")
    published = load_published()
    posted_count = 0
    for news in news_candidates:
        if posted_count >= config.POSTS_PER_RUN:
            break
        print(f"\n⚙️ Пробую: {news['title'][:50]}...")
        text = rewrite(news)
        if not text:
            print(f"⏭️ Пропускаю: {news['title'][:40]}")
            mark_published(published, news)
            save_published(published)
            continue
        print(f"📝 Текст: {len(text)} символов")
        ip = generate_image_prompt(news)
        img = generate_image(ip, news['id']) if ip else None
        tg = post_tg(text, img)
        time.sleep(2)
        vk = post_vk(text)
        print(f"TG: {tg} | VK: {vk} | Img: {bool(img)}")
        if tg or vk:
            mark_published(published, news)
            save_published(published)
            save_to_site(news, text, img)
            posted_count += 1
        if posted_count < config.POSTS_PER_RUN:
            time.sleep(config.DELAY_BETWEEN_POSTS)
    print(f"\n✅ Готово: {datetime.now()} | Опубликовал {posted_count} постов")

if __name__ == "__main__":
    main()
