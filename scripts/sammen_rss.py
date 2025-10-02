#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Generer uoffisiell RSS-feed fra https://sammen.no/no/artikkel
Tilpasset for "Custom RSS"-skjermapp (bilder, ingress, begrenset antall elementer).
Krever: requests, beautifulsoup4
"""

import re
import sys
import time
import json
import html
import argparse
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from email.utils import format_datetime
import xml.etree.ElementTree as ET


def parse_args():
    p = argparse.ArgumentParser(description="Lag RSS fra Sammen-artikler.")
    p.add_argument("--list-url", default="https://sammen.no/no/artikkel")
    p.add_argument("--base", default="https://sammen.no")
    p.add_argument("--out", default="public/rss.xml")
    p.add_argument("--max-items", type=int, default=10)
    p.add_argument("--refresh-minutes", type=int, default=30)
    p.add_argument("--default-image", default="")
    p.add_argument("--timeout", type=int, default=15)
    p.add_argument("--delay", type=float, default=0.4)
    p.add_argument("--lang", default="no,en;q=0.8")
    p.add_argument("--user-agent", default="SammenRSSBot/1.1 (+contact: rssbot@example.org)")
    return p.parse_args()


class Fetcher:
    def __init__(self, ua, lang, timeout):
        self.sess = requests.Session()
        self.sess.headers.update({"User-Agent": ua, "Accept-Language": lang})
        self.timeout = timeout

    def get_text(self, url):
        r = self.sess.get(url, timeout=self.timeout)
        r.raise_for_status()
        return r.text

    def get_head(self, url):
        try:
            r = self.sess.head(url, timeout=self.timeout, allow_redirects=True)
            r.raise_for_status()
            return r.headers
        except Exception:
            return {}


def to_abs(base, url):
    return url if url.startswith("http") else urljoin(base, url)


def clean_text(s):
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def truncate(txt, max_len=240):
    txt = clean_text(txt)
    return (txt[: max_len - 1] + "…") if len(txt) > max_len else txt


def parse_list(list_url, base, fetcher):
    html_doc = fetcher.get_text(list_url)
    soup = BeautifulSoup(html_doc, "html.parser")
    scope = soup.select_one("main") or soup

    hrefs = []
    # Fang opp både relative og absolutte lenker til /no/artikkel/
    for a in scope.select('a[href*="/no/artikkel/"]'):
        href = a.get("href", "")
        u = to_abs(base, href)  # normaliser til absolutt
        path = urlparse(u).path
        if path and path.startswith("/no/artikkel/") and path.strip("/") != "no/artikkel":
            hrefs.append(u)

    # Fjern duplikater, behold rekkefølge
    seen = set()
    unique = []
    for u in hrefs:
        p = urlparse(u).path
        if p not in seen:
            seen.add(p)
            unique.append(u)
    return unique


def parse_iso_date(s):
    if not s:
        return None
    try:
        s = s.strip().replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except Exception:
        return None


def parse_date_from_jsonld(soup):
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except Exception:
            continue

        def extract(d):
            if isinstance(d, dict):
                if "datePublished" in d:
                    return parse_iso_date(d.get("datePublished"))
                for k in ("article", "mainEntityOfPage"):
                    if isinstance(d.get(k), dict):
                        dt = d[k].get("datePublished")
                        if dt:
                            return parse_iso_date(dt)
            elif isinstance(d, list):
                for it in d:
                    dt = extract(it)
                    if dt:
                        return dt
            return None

        dt = extract(data)
        if dt:
            return dt
    return None


def parse_article(url, base, fetcher, default_image=""):
    try:
        html_doc = fetcher.get_text(url)
    except Exception:
        return None

    soup = BeautifulSoup(html_doc, "html.parser")

    # Tittel
    title = None
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(strip=True)
    if not title:
        ogt = soup.find("meta", property="og:title")
        if ogt and ogt.get("content"):
            title = ogt["content"]
    if not title and soup.title:
        title = soup.title.get_text(strip=True)
    if not title:
        title = url

    # Ingress
    desc = None
    md = soup.find("meta", attrs={"name": "description"})
    if md and md.get("content"):
        desc = md["content"]
    if not desc:
        ogd = soup.find("meta", property="og:description")
        if ogd and ogd.get("content"):
            desc = ogd["content"]
    if not desc:
        main = soup.select_one("main") or soup
        p = main.find("p")
        if p:
            desc = p.get_text(" ", strip=True)
    desc = truncate(desc or "")

    # Bilde
    image = None
    og_img = soup.find("meta", property="og:image")
    if og_img and og_img.get("content"):
        image = to_abs(base, og_img["content"])
    if not image and default_image:
        image = default_image

    # Dato
    pub = None
    meta_time = soup.find("meta", property="article:published_time")
    if meta_time and meta_time.get("content"):
        pub = parse_iso_date(meta_time["content"])

    if not pub:
        time_tag = soup.find("time", attrs={"datetime": True})
        if time_tag:
            pub = parse_iso_date(time_tag["datetime"])

    if not pub:
        pub = parse_date_from_jsonld(soup)

    if not pub:
        heads = fetcher.get_head(url)
        lm = heads.get("Last-Modified")
        if lm:
            try:
                pub = datetime.strptime(lm, "%a, %d %b %Y %H:%M:%S %Z").replace(tzinfo=timezone.utc)
            except Exception:
                pub = None

    return {
        "title": title,
        "link": url,
        "description": desc,
        "image": image,
        "pubDate": pub,
        "guid": url,
    }


def build_rss(items, list_url, refresh_minutes):
    NS_MEDIA = "http://search.yahoo.com/mrss/"
    ET.register_namespace("media", NS_MEDIA)

    rss = ET.Element("rss", attrib={"version": "2.0", "xmlns:media": NS_MEDIA})
    chan = ET.SubElement(rss, "channel")
    ET.SubElement(chan, "title").text = "Sammen – Artikler (uoffisiell RSS)"
    ET.SubElement(chan, "link").text = list_url
    ET.SubElement(chan, "description").text = "Automatisk generert feed fra /no/artikkel for skjermvisning"
    ET.SubElement(chan, "lastBuildDate").text = format_datetime(datetime.now(timezone.utc))
    ET.SubElement(chan, "ttl").text = str(refresh_minutes)

    for it in items:
        i = ET.SubElement(chan, "item")
        ET.SubElement(i, "title").text = it["title"]
        ET.SubElement(i, "link").text = it["link"]
        g = ET.SubElement(i, "guid")
        g.set("isPermaLink", "true")
        g.text = it["guid"]
        if it["pubDate"]:
            ET.SubElement(i, "pubDate").text = format_datetime(it["pubDate"])
        ET.SubElement(i, "description").text = html.escape(it["description"] or "")

        if it["image"]:
            # MRSS
            ET.SubElement(i, "{%s}content" % NS_MEDIA, attrib={"url": it["image"], "medium": "image"})
            # Enclosure (fallback)
            enc = ET.SubElement(i, "enclosure")
            enc.set("url", it["image"])
            if it["image"].lower().endswith(".png"):
                enc.set("type", "image/png")
            elif it["image"].lower().endswith(".webp"):
                enc.set("type", "image/webp")
            else:
                enc.set("type", "image/jpeg")

    return ET.ElementTree(rss)


def main():
    args = parse_args()
    fetcher = Fetcher(args.user_agent, args.lang, args.timeout)

    # 1) Hent artikkelliste
    links = parse_list(args.list_url, args.base, fetcher)

    # 2) Besøk artikler og bygg items
    items = []
    for url in links:
        art = parse_article(url, args.base, fetcher, default_image=args.default_image)
        if art:
            items.append(art)
        time.sleep(args.delay)

    # 3) Sorter nyeste først
    items.sort(key=lambda x: (x["pubDate"] is not None, x["pubDate"] or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)

    # 4) Begrens antall
    items = items[: max(1, args.max_items)]

    # 5) Skriv RSS
    tree = build_rss(items, args.list_url, args.refresh_minutes)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    tree.write(args.out, encoding="utf-8", xml_declaration=True)
    print(f"✅ Skrev {len(items)} items til {args.out}")


if __name__ == "__main__":
    main()
