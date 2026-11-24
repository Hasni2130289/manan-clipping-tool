"""Manan Clipping Tool — site configuration.

SOCIAL_LINKS is the single place where the app's social links live.
The frontend fetches them from GET /api/social-links, which also
returns the brand line, tagline, and WhatsApp contact row. The login
page (web/login.html) mirrors the same list inline because it loads
before any session exists — if you change a link here, update that
mirror too.
"""

BRAND_LINE = "MANAN AUTOMATION — Automation • AI • Digital Services"
TAGLINE = "AI Tools • SMM Services • Digital Solutions"
CONTACT_PHONE = "+92 371 4675373"
CONTACT_URL = "https://wa.me/923714675373"

SOCIAL_LINKS = [
    {"name": "WhatsApp", "url": "https://wa.me/923714675373"},
    {"name": "WhatsApp Channel", "url": "https://whatsapp.com/channel/0029VbDjt8KElagtzfMOaS1x"},
    {"name": "WhatsApp Group", "url": "https://chat.whatsapp.com/Ck1PQHKwNzUBWgOCmYgT6x?s=dl&p=a&lr=4&iam=2"},
    {"name": "Facebook", "url": "https://www.facebook.com/share/1GNAaoYFco/"},
    {"name": "TikTok", "url": "https://www.tiktok.com/@mananautomation"},
    {"name": "Instagram", "url": "https://www.instagram.com/mananautomationofficial"},
    {"name": "YouTube", "url": "https://youtube.com/@mananautomation-28?si=l9En5P7Csk-rEEpa"},
    {"name": "SMM Services Website", "url": "https://manansmm.com"},
    {"name": "AI Tools & Online Store", "url": "https://onlinestore.manansmm.com"},
]
