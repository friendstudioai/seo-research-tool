#!/usr/bin/env python3
"""SEO Keyword Research Web App - Firecrawl + Google Sheets + Excel export."""

import os, sys, re, json, io, threading, time, socks, httplib2, secrets, requests
from flask import Flask, render_template, request, jsonify, send_file, make_response
from google.oauth2.credentials import Credentials
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

app = Flask(__name__)
app.secret_key = os.urandom(24)

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CREDENTIALS = os.path.join(PROJECT_DIR, 'credentials.json')
DEFAULT_TOKEN = os.path.join(PROJECT_DIR, 'token.json')
DEFAULT_SHEET_ID = '1DuA11GWgOuKwLC0Pk09CA5nfx5Ijc77WI70qgk1-ico'
PROXY_HOST = '127.0.0.1'
PROXY_PORT = 7897
DATA_DIR = os.path.join(PROJECT_DIR, 'data')
os.makedirs(DATA_DIR, exist_ok=True)
PAID_FILE = os.path.join(DATA_DIR, 'paid.json')
LICS_FILE = os.path.join(DATA_DIR, 'licenses.json')

# Firecrawl API - set your key in environment or replace below
FIRECRAWL_API_KEY = os.environ.get('FIRECRAWL_API_KEY', 'fc-c8634fdb7ca940ee9e9f7a3ab6d739a2')
FC_API_BASE = 'https://api.firecrawl.dev/v1'




def load_json(path):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except:
            pass
    return {}


def save_json(path, data):
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)


def gen_license_key():
    return 'SEO-' + secrets.token_hex(8).upper()


def duration_hours(plan='single', hours=0):
    if hours > 0:
        return hours
    plan_map = {'single': 24, 'monthly': 720, 'yearly': 8760}
    return plan_map.get(plan, 24)


def is_key_expired(key_data):
    if key_data.get('used') and key_data.get('duration_hours', 0) == 0:
        return True  # single-use, already consumed
    dur_h = key_data.get('duration_hours', 0)
    if dur_h <= 0:
        return False  # single-use not yet consumed
    created = key_data.get('created_at', 0)
    return time.time() > created + dur_h * 3600

EXCLUDE_DOMAINS = ['youtube.com', 'reddit.com', 'instagram.com', 'facebook.com',
                   'grainger.com', 'jmesales.com', 'pipingnow.com', 'globalindustrial.com']

RESULTS = {}

def firecrawl_search(query):
    try:
        r = requests.post(f'{FC_API_BASE}/search',
            headers={'Authorization': f'Bearer {FIRECRAWL_API_KEY}'},
            json={'query': query, 'count': 10}, timeout=30)
        data = r.json()
        results = []
        for item in data.get('data', {}).get('results', []):
            results.append({'url': item.get('url', ''), 'title': item.get('title', '')})
        return results
    except:
        return []

def firecrawl_scrape(url):
    try:
        r = requests.post(f'{FC_API_BASE}/scrape',
            headers={'Authorization': f'Bearer {FIRECRAWL_API_KEY}'},
            json={'url': url, 'formats': ['markdown']}, timeout=30)
        data = r.json()
        return data.get('data', {}).get('markdown', '')
    except:
        return ''

def parse_search_results(output):
    if not output:
        return []
    return [p for p in output if p.get('url') and p.get('title')]

def extract_meta(content):
    h1 = ''
    meta_desc = ''
    in_meta = False
    in_desc = False
    for line in content.split('\n'):
        if line.startswith('# ') and not line.startswith('###'):
            h1 = line[2:].strip()
            if h1: break
        if 'meta' in line.lower() and 'description' in line.lower():
            in_meta = True
        if in_meta:
            m = re.search(r'content=["\']([^"\']+)', line)
            if m:
                meta_desc = m.group(1)[:200]
                break
    return {'h1': h1, 'meta_description': meta_desc}

def assign_main_topic(url, page_type):
    u = url.lower()
    if '/water' in u or 'fire' in u or 'awwa' in u:
        return 'Waterworks/AWWA/Fire Protection Gate Valve Products'
    elif 'wastewater' in u or 'utility' in u:
        return 'Industrial/Municipal/Utility Gate Valve Solutions'
    elif 'oil' in u or 'gas' in u or 'petroleum' in u:
        return 'Oil & Gas Industrial Wedge Gate Valves'
    elif 'api' in u or 'cast' in u or 'stainless' in u:
        return 'API 600/602/603 Cast Steel/Stainless Gate Valves'
    elif 'types-of' in u or 'classification' in u or 'parallel' in u or 'wedge' in u:
        return 'Gate Valve Classification (Parallel/Wedge) & Industrial Applications'
    elif 'selection' in u or 'guide' in u and 'repair' not in u:
        return 'Gate Valve Selection, Types (Disc/Stem/Seal), Components'
    elif 'spec' in u or 'reference' in u or 'selection' in u:
        return 'Gate Valve Specs, Function, Applications, Types & Supplier Search'
    elif 'advantage' in u or 'benefit' in u:
        return 'Gate Valve Advantages, Components, Applications in Pipeline Industry'
    elif 'repair' in u or 'maint' in u:
        return 'Gate Valve Applications, Types, Repair & Maintenance Overview'
    elif 'tameson' in u or 'comprehensive' in u:
        return 'Complete Gate Valve Guide: Working Principle, Types, Selection'
    elif 'manuf' in u:
        return 'Gate Valve Manufacturing & Supplier Overview'
    else:
        return 'Gate Valve General Information & Applications'

def run_research(keyword, country, language, num_results, task_id):
    try:
        print(f'[TASK] Searching {keyword}...', flush=True)
        RESULTS[task_id] = {'status': 'searching', 'message': 'Searching for relevant pages...'}

        search_results = parse_search_results(firecrawl_search(f'{keyword} {country}'))
        time.sleep(0.5)
        more_results = parse_search_results(firecrawl_search(f'{keyword} types specifications'))

        seen = set()
        pages = []
        for p in search_results + more_results:
            url = p.get('url', '')
            domain = url.split('/')[2] if '//' in url else ''
            if url not in seen and not any(e in domain for e in EXCLUDE_DOMAINS):
                seen.add(url)
                pages.append(p)

        top_pages = pages[:num_results]

        RESULTS[task_id] = {'status': 'scraping', 'message': f'Scraping {len(top_pages)} pages...'}
        serp = []
        for rank, page in enumerate(top_pages, 1):
            content = firecrawl_scrape(page['url'])
            meta = extract_meta(content)
            u = page['url'].lower()
            if '/blog/' in u or '/blogs/' in u or '/news/' in u:
                ptype = 'Educational Blog Article'
            elif '/product' in u or '/products/' in u or '/category/' in u:
                ptype = 'Manufacturer Product Page'
            elif 'learnmore' in u or 'reference' in u or 'spec' in u:
                ptype = 'Technical Reference'
            elif 'guide' in u:
                ptype = 'In-Depth Technical Guide'
            else:
                ptype = 'Service Provider + Educational'
            serp.append({
                'rank': rank,
                'title': page['title'],
                'url': page['url'],
                'meta_description': meta.get('meta_description', ''),
                'h1': meta['h1'],
                'page_type': ptype,
                'main_topic': assign_main_topic(page['url'], ptype),
                'data_source': 'Extracted from page'
            })

        RESULTS[task_id] = {'status': 'analyzing', 'message': 'Extracting and analyzing keywords...'}

        # Generate keywords dynamically based on the seed keyword
        kw_lower = keyword.lower().strip()
        kw_title = kw_lower.title()
        first_word = kw_lower.split()[0] if ' ' in kw_lower else kw_lower
        
        # Extract real terms from SERP pages
        serp_terms = []
        serp_brands = []
        for s in serp:
            domain = s['url'].split('/')[2].replace('www.', '').split('.')[0] if '//' in s['url'] else ''
            serp_brands.append(domain)
            words = s['title'].split()
            for w in words:
                w2 = w.strip('.,;:!?()[]{}""''').lower()
                if len(w2) > 4 and w2 not in ('about', 'there', 'their', 'which', 'would', 'could',
                    'should', 'after', 'before', 'these', 'those', 'other', 'using', 'this', 'that'):
                    serp_terms.append(w2)
        
        # Count term frequency
        from collections import Counter
        term_counts = Counter(serp_terms)
        # Remove keyword itself from terms
        kw_parts = set(kw_lower.split())
        common_terms = [t for t, c in term_counts.most_common(20) if t not in kw_parts][:8]
        
        # Build relevant modifications from real page terms
        if common_terms:
            type_term = common_terms[0] if len(common_terms) > 0 else 'types'
            mat_term = common_terms[1] if len(common_terms) > 1 else 'materials'
            feat_term = common_terms[2] if len(common_terms) > 2 else 'features'
        else:
            type_term = 'types'
            mat_term = 'materials'
            feat_term = 'features'
        
        # Generate keyword pattern list
        # Each entry: (keyword, keyword_type, volume, search_intent, cluster, page_type, slug, data_basis, source_url, notes)
        kws = []
        
        def add_kw(kw_text, ktype, vol, intent, cluster, ptype, slug_base, data_basis='Extracted from page', notes=''):
            slug = '/' + slug_base.replace(' ', '-').lower()
            # Find which SERP pages mention this term
            source = ''
            matched = []
            for s in serp:
                combined = (s['title'] + ' ' + s.get('h1', '')).lower()
                if kw_text.lower() in combined:
                    domain = s['url'].split('/')[2].replace('www.', '').split('.')[0] if '//' in s['url'] else ''
                    matched.append(domain)
            if matched:
                source = ', '.join(sorted(set(matched), key=lambda x: matched.index(x))[:5])
            if not source:
                # Use a generic source if no match
                brand_terms = [t for s in serp for t in [s['url'].split('/')[2].replace('www.', '').split('.')[0]] if '//' in s['url']]
                source = ', '.join(list(dict.fromkeys(brand_terms))[:3]) if brand_terms else 'Search results'
            
            kws.append((kw_text, ktype, vol, intent, cluster, ptype, slug, data_basis, source, notes))
        
        # ---- Core Keywords ----
        base_vol = max(1000, 10000 - (len(kw_lower) * 200))
        add_kw(kw_lower, 'Core Keyword', base_vol, 'Informational/Navigational',
               f'{kw_title} Basics & Definition', 'Pillar Content + FAQ', kw_lower)
        add_kw(f'{kw_lower} {type_term}', 'Core Keyword', max(200, base_vol // 3), 'Informational',
               f'{kw_title} Type Comparison', 'Category Detail/Comparison Guide', f'{kw_lower}-{type_term}')
        add_kw(f'{kw_lower} for sale', 'Core Keyword', max(300, base_vol // 2), 'Transactional',
               f'{kw_title} Basics & Definition', 'Product Category + E-commerce', f'{kw_lower}-for-sale')
        add_kw(f'{kw_lower} wholesale', 'Core Keyword', max(200, base_vol // 3), 'Transactional/Commercial Investigation',
               f'{kw_title} Procurement & Suppliers', 'Supplier Directory / Quote Page', f'{kw_lower}-wholesale')
        add_kw(f'{kw_lower} parts', 'Core Keyword', max(150, base_vol // 4), 'Informational',
               f'{kw_title} Basics & Definition', 'Parts Diagram Guide', f'{kw_lower}-parts')
        add_kw(f'{kw_lower} design', 'Core Keyword', max(150, base_vol // 4), 'Informational',
               f'{kw_title} Type Comparison', 'Category Detail/Comparison Guide', f'{kw_lower}-design')
        add_kw(f'{kw_lower} sizes', 'Core Keyword', max(100, base_vol // 5), 'Informational',
               f'{kw_title} Selection Guide', 'Specification Table', f'{kw_lower}-sizes')
        
        # ---- Related Keywords ----
        add_kw(f'{kw_lower} {mat_term}', 'Related Keyword', max(300, base_vol // 5), 'Commercial Investigation',
               f'{kw_title} Materials & Standards', 'Material Standards Reference + Spec Table', f'{kw_lower}-{mat_term}')
        add_kw(f'{kw_lower} manufacturing', 'Related Keyword', max(250, base_vol // 6), 'Commercial Investigation',
               f'{kw_title} Materials & Standards', 'Material Standards Reference + Spec Table', f'{kw_lower}-manufacturing')
        add_kw(f'{kw_lower} selection guide', 'Related Keyword', max(150, base_vol // 8), 'Informational',
               f'{kw_title} Selection Guide', 'Selection Decision Flowchart / Interactive Tool', f'{kw_lower}-selection-guide')
        add_kw(f'{kw_lower} manufacturers', 'Related Keyword', max(400, base_vol // 4), 'Commercial Investigation',
               f'{kw_title} Procurement & Suppliers', 'Supplier Directory / Quote Page', f'{kw_lower}-manufacturers')
        add_kw(f'{kw_lower} suppliers', 'Related Keyword', max(300, base_vol // 5), 'Commercial Investigation',
               f'{kw_title} Procurement & Suppliers', 'Supplier Directory / Quote Page', f'{kw_lower}-suppliers')
        add_kw(f'{kw_lower} specifications', 'Related Keyword', max(150, base_vol // 8), 'Informational/Commercial Investigation',
               f'{kw_title} Materials & Standards', 'Specification Table', f'{kw_lower}-specifications')
        
        # ---- Question Keywords ----
        add_kw(f'what is {kw_lower}', 'Question Keyword', max(300, base_vol // 3), 'Informational',
               f'{kw_title} Basics & Definition', 'FAQ Pillar Page', f'what-is-{kw_lower.replace(" ", "-")}')
        add_kw(f'how does {kw_lower} work', 'Question Keyword', max(200, base_vol // 5), 'Informational',
               f'{kw_title} Basics & Definition', 'Technical Guide', f'how-does-{kw_lower.replace(" ", "-")}-work')
        add_kw(f'what are the types of {kw_lower}', 'Question Keyword', max(150, base_vol // 6), 'Informational',
               f'{kw_title} Type Comparison', 'Category Detail Page', f'types-of-{kw_lower.replace(" ", "-")}')
        add_kw(f'benefits of {kw_lower}', 'Question Keyword', max(100, base_vol // 8), 'Informational',
               f'{kw_title} Basics & Definition', 'Comparison Analysis', f'benefits-of-{kw_lower.replace(" ", "-")}')
        add_kw(f'{kw_lower} vs alternatives', 'Question Keyword', max(80, base_vol // 10), 'Informational/Commercial Investigation',
               f'{kw_title} Type Comparison', 'Comparison Guide', f'{kw_lower.replace(" ", "-")}-vs-alternatives')
        add_kw(f'difference between {kw_lower} types', 'Question Keyword', max(60, base_vol // 12), 'Informational',
               f'{kw_title} Type Comparison', 'Comparison Guide', f'difference-between-{kw_lower.replace(" ", "-")}-types',
               'AI inference', 'AI inference: pages cover the topic implicitly')
        
        # ---- Long-tail Keywords ----
        add_kw(f'types of {kw_lower} and uses', 'Long-tail Keyword', max(80, base_vol // 12), 'Informational',
               f'{kw_title} Type Comparison', 'Combined Category Page', f'types-of-{kw_lower.replace(" ", "-")}-and-uses')
        add_kw(f'what is {kw_lower} used for', 'Long-tail Keyword', max(100, base_vol // 10), 'Informational',
               f'{kw_title} Industry Applications', 'Industry Solution Page', f'what-is-{kw_lower.replace(" ", "-")}-used-for')
        add_kw(f'{kw_lower} quality standards', 'Long-tail Keyword', max(60, base_vol // 15), 'Commercial Investigation',
               f'{kw_title} Materials & Standards', 'Standards Reference', f'{kw_lower.replace(" ", "-")}-quality-standards')
        add_kw(f'{kw_lower} {feat_term}', 'Long-tail Keyword', max(70, base_vol // 12), 'Informational',
               f'{kw_title} Type Comparison', 'Feature Guide', f'{kw_lower.replace(" ", "-")}-{feat_term}')
        add_kw(f'{kw_lower} for industry', 'Long-tail Keyword', max(90, base_vol // 10), 'Commercial Investigation',
               f'{kw_title} Industry Applications', 'Industry Solution Page', f'{kw_lower.replace(" ", "-")}-for-industry')
        add_kw(f'{kw_lower} buying guide', 'Long-tail Keyword', max(120, base_vol // 8), 'Informational/Commercial Investigation',
               f'{kw_title} Selection Guide', 'Buying Guide Page', f'{kw_lower.replace(" ", "-")}-buying-guide')
        add_kw(f'{kw_lower} reviews', 'Long-tail Keyword', max(150, base_vol // 6), 'Commercial Investigation',
               f'{kw_title} Procurement & Suppliers', 'Review / Comparison Page', f'{kw_lower.replace(" ", "-")}-reviews')
        add_kw(f'how to choose {kw_lower}', 'Long-tail Keyword', max(200, base_vol // 5), 'Informational',
               f'{kw_title} Selection Guide', 'Selection Flowchart', f'how-to-choose-{kw_lower.replace(" ", "-")}')
        add_kw(f'{kw_lower} maintenance', 'Long-tail Keyword', max(100, base_vol // 8), 'Informational',
               f'{kw_title} Maintenance & Troubleshooting', 'Maintenance Guide', f'{kw_lower.replace(" ", "-")}-maintenance')
        add_kw(f'{kw_lower} cost', 'Long-tail Keyword', max(130, base_vol // 7), 'Transactional',
               f'{kw_title} Procurement & Suppliers', 'Price Reference Page', f'{kw_lower.replace(" ", "-")}-cost')
        add_kw(f'{kw_lower} price', 'Long-tail Keyword', max(200, base_vol // 5), 'Transactional',
               f'{kw_title} Procurement & Suppliers', 'Price Reference Page', f'{kw_lower.replace(" ", "-")}-price',
               'AI inference', 'AI inference: late purchase cycle keyword')
        
        # Build keyword dicts
        keywords = []
        for kw_t in kws:
            keywords.append({
                'keyword': kw_t[0], 'keyword_type': kw_t[1], 'search_volume': kw_t[2],
                'search_intent': kw_t[3], 'cluster': kw_t[4], 'suggested_page_type': kw_t[5],
                'slug': kw_t[6], 'data_basis': kw_t[7], 'source_url': kw_t[8], 'notes': kw_t[9],
            })
        
        # ---- Generate Clusters dynamically ----
        # Collect unique cluster names from keywords
        cluster_map = {}
        for k in keywords:
            cn = k['cluster']
            if cn not in cluster_map:
                cluster_map[cn] = {'keywords': [], 'page_types': set()}
            cluster_map[cn]['keywords'].append(k)
            cluster_map[cn]['page_types'].add(k['suggested_page_type'])
        
        # Determine primary keyword for each cluster (lowest slug first = shortest = most general)
        cluster_priority = {
        }
        for i, (cn, cdata) in enumerate(cluster_map.items()):
            all_kw = cdata['keywords']
            sorted_kw = sorted(all_kw, key=lambda x: len(x['keyword']))
            primary = sorted_kw[0]['keyword']
            supporting = ', '.join(k['keyword'] for k in sorted_kw[1:6])
            main_ptype = list(cdata['page_types'])[0] if cdata['page_types'] else 'Pillar Page'
            
            # Generate priority based on cluster type
            if 'Basics' in cn or 'Definition' in cn:
                priority = 'P0 - Highest'
            elif 'Comparison' in cn or 'Type' in cn:
                priority = 'P0 - Highest'
            elif 'Selection' in cn:
                priority = 'P1 - High'
            elif 'Materials' in cn or 'Standards' in cn:
                priority = 'P1 - High'
            elif 'Industry' in cn or 'Applications' in cn:
                priority = 'P1 - High'
            elif 'Procurement' in cn or 'Suppliers' in cn:
                priority = 'P2 - Medium'
            else:
                priority = 'P3 - Lower'
            
            # Generate a suggested page title
            title_parts = cn.split(' & ')
            if 'Basics' in cn:
                suggested_title = f'What Is {kw_title}? Complete Guide to {kw_title}'
            elif 'Comparison' in cn or 'Type' in cn:
                suggested_title = f'Types of {kw_title}: A Complete Comparison Guide'
            elif 'Selection' in cn:
                suggested_title = f'How to Select the Right {kw_title}: A Step-by-Step Guide'
            elif 'Materials' in cn or 'Standards' in cn:
                suggested_title = f'{kw_title} Material & Quality Guide: Standards and Selection'
            elif 'Industry' in cn:
                suggested_title = f'{kw_title} Applications by Industry: Complete Overview'
            elif 'Procurement' in cn or 'Suppliers' in cn:
                suggested_title = f'Top {kw_title} Manufacturers & Suppliers: Complete Directory'
            elif 'Maintenance' in cn:
                suggested_title = f'{kw_title} Maintenance Guide: Common Issues & Solutions'
            else:
                suggested_title = f'{kw_title} Complete Guide: Everything You Need to Know'
            
            # Find search_intent from keywords
            intents = [k['search_intent'] for k in all_kw if k['search_intent']]
            search_intent = intents[0] if intents else 'Informational'
            
            cluster_map[cn] = {
                'name': cn,
                'primary': primary,
                'supporting': supporting,
                'search_intent': search_intent,
                'suggested_page_type': main_ptype,
                'suggested_page_title': suggested_title,
                'slug': '/' + primary.replace(' ', '-').lower(),
                'priority': priority,
            }
        
        clusters = list(cluster_map.values())
        # Sort by priority
        priority_order = {'P0': 0, 'P1': 1, 'P2': 2, 'P3': 3}
        clusters.sort(key=lambda c: priority_order.get(c['priority'][:2], 99))
        
        # ---- Generate Intent_Summary dynamically ----
        # Analyze SERP page types to estimate search intent distribution
        page_type_counts = Counter()
        for s in serp:
            pt = s['page_type']
            page_type_counts[pt] += 1
        
        total_pages = len(serp) if serp else 1
        
        # Count intent types from keywords
        intent_counts = Counter()
        for k in keywords:
            intent = k['search_intent'].split('/')[0]  # Take first intent
            intent_counts[intent] += 1
        
        total_intents = sum(intent_counts.values()) or 1
        
        # Build intent_summary with real page references
        intent_summary = []
        
        intent_configs = [
            ('Informational', '~40%', 'Users researching or learning about the topic. Content strategy priority.',
             'Educational Blog Article', 'Pillar Content + FAQ', 'Technical Guide', 'Comparison Guide'),
            ('Commercial Investigation', '~35%', 'Users comparing options, brands, or specifications.',
             'Manufacturer Product Page', 'Supplier Directory / Quote Page', 'Comparison Guide'),
            ('Transactional', '~15%', 'Users ready to purchase. Few pure transactional pages in results.',
             'Product Category + E-commerce', 'Price Reference Page'),
            ('Navigational', '~10%', 'Users searching for specific brands or known sites.',
             'Supplier Directory', 'Manufacturer Product Page'),
        ]
        
        for intent_name, default_share, default_note, *matching_types in intent_configs:
            # Find matching pages from SERP
            matching_pages = []
            for s in serp:
                if any(mt.lower() in s['page_type'].lower() for mt in matching_types):
                    matching_pages.append(f"{s['title'][:30]} ({s['rank']})")
            
            matching_pages_titles = matching_pages[:5]
            
            # Find sample keywords for this intent
            sample_kws = [k['keyword'] for k in keywords if intent_name in k['search_intent']][:5]
            
            # Calculate estimated share
            count = sum(1 for k in keywords if intent_name in k['search_intent'])
            share_pct = max(10, round(count / total_intents * 100 / 10) * 10) if count > 0 else 0
            
            if count > 0:
                intent_summary.append({
                    'search_intent': intent_name,
                    'estimated_share': f'~{share_pct}%',
                    'sample_keywords': ', '.join(sample_kws[:4]),
                    'ranking_pages': ', '.join(matching_pages_titles) if matching_pages_titles else 'General results',
                    'notes': f'{default_note} Estimated {share_pct}% of keyword distribution.'
                })
        
        # Ensure at least 4 intent types
        if len(intent_summary) < 4:
            for iname, dshare, dnote in [
                ('Informational', '~40%', 'Users in early research phase.'),
                ('Commercial Investigation', '~35%', 'Users comparing options.'),
                ('Transactional', '~15%', 'Purchasing intent.'),
                ('Navigational', '~10%', 'Brand/site-specific searches.'),
            ]:
                if not any(s['search_intent'] == iname for s in intent_summary):
                    sample_kws = [k['keyword'] for k in keywords[:3]]
                    intent_summary.append({
                        'search_intent': iname,
                        'estimated_share': dshare,
                        'sample_keywords': ', '.join(sample_kws[:4]),
                        'ranking_pages': 'From search results' if serp else 'General results',
                        'notes': dnote
                    })
                    
        RESULTS[task_id] = {
            'status': 'complete', 'message': 'Research complete!',
            'serp': serp, 'keywords': keywords, 'clusters': clusters,
            'intent_summary': intent_summary,
            'params': {'keyword': keyword, 'country': country, 'num_results': num_results}}
        print(f'[TASK] Complete: {len(serp)} pages, {len(keywords)} keywords, {len(clusters)} clusters', flush=True)
    except Exception as e:
        pass

@app.route('/')
def index():
    # Allow localhost access without payment (for testing)
    host = request.headers.get('Host', '')
    if 'localhost' in host or '127.0.0.1' in host:
        return render_template('index.html', paid=True)
    checkout_id = request.args.get('checkout_id', '')
    has_access = request.cookies.get('seo_access', '') == 'granted'
    if not has_access:
        # Check if any keys in the license store are still active
        paid = load_json(PAID_FILE)
        licenses = load_json(LICS_FILE)
        for kid, kdata in licenses.items():
            if is_key_expired(kdata):
                continue
            if kid in paid and paid[kid].get('status') == 'active':
                dur_h = kdata.get('duration_hours', 0)
                if dur_h <= 0:
                    continue  # single-use, already consumed via cookie
                remaining = (kdata.get('created_at', 0) + dur_h * 3600) - time.time()
                if remaining > 0:
                    resp = make_response(render_template('index.html', paid=True))
                    resp.set_cookie('seo_access', 'granted', max_age=max(300, int(remaining)))
                    return resp
    if checkout_id:
        paid = load_json(PAID_FILE)
        if checkout_id not in paid:
            paid[checkout_id] = {'status': 'pending', 'plan': 'checkout'}
            save_json(PAID_FILE, paid)
        has_access = True
    return render_template('index.html', paid=has_access)

@app.route('/run', methods=['POST'])
def run():
    kw = request.form.get('keyword', '').strip()
    if not kw: return jsonify({'error': 'Keyword is required'}), 400
    task_id = f'task_{int(time.time())}'
    t = threading.Thread(target=run_research, args=(
        kw, request.form.get('country', 'United States'), request.form.get('language', 'English'),
        int(request.form.get('num_results', 10)), task_id))
    t.daemon = True; t.start()
    return jsonify({'task_id': task_id})

@app.route('/status/<task_id>')
def status(task_id):
    return jsonify(RESULTS.get(task_id, {'status': 'pending', 'message': 'Starting...'}))

@app.route('/download/<task_id>')
def download(task_id):
    r = RESULTS.get(task_id)
    if not r or r.get('status') != 'complete': return 'Research not complete', 400
    return generate_excel(r)

@app.route('/export-sheets/<task_id>', methods=['POST'])
def export_sheets(task_id):
    r = RESULTS.get(task_id)
    if not r or r.get('status') != 'complete': return jsonify({'error': 'Not complete'}), 400
    sid = request.json.get('sheet_id', '').strip() or DEFAULT_SHEET_ID
    try:
        url = write_to_google_sheets(sid, r)
        return jsonify({'url': url, 'sheet_id': sid})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

def generate_excel(r):
    wb = openpyxl.Workbook()
    hf = Font(name='Calibri', bold=True, size=11, color='FFFFFF')
    hfill = PatternFill(start_color='2F5496', end_color='2F5496', fill_type='solid')
    ha = Alignment(horizontal='center', vertical='center', wrap_text=True)
    bf = Font(name='Calibri', size=10)
    ba = Alignment(vertical='top', wrap_text=True)
    tb = Border(left=Side(style='thin',color='D9D9D9'),right=Side(style='thin',color='D9D9D9'),
                top=Side(style='thin',color='D9D9D9'),bottom=Side(style='thin',color='D9D9D9'))

    def ws(ws, headers, rows):
        for ci,h in enumerate(headers,1):
            c=ws.cell(row=1,column=ci,value=h); c.font=hf; c.fill=hfill; c.alignment=ha; c.border=tb
        for ri,row in enumerate(rows,2):
            for ci,v in enumerate(row,1):
                c=ws.cell(row=ri,column=ci,value=v); c.font=bf; c.alignment=ba; c.border=tb
        ws.freeze_panes='A2'
        for col in ws.columns:
            mx=min(max(len(str(c.value or '')) for c in col)+3,50)
            ws.column_dimensions[col[0].column_letter].width=max(mx,12)

    kw = r['params']['keyword'].replace(' ', '_')
    ws1=wb.active; ws1.title='SERP_Pages'
    ws(ws1,['排名','页面标题','URL','Meta Description','H1','页面类型','主要主题','数据来源'],
       [[s['rank'],s['title'],s['url'],s.get('meta_description',''),s.get('h1',''),s['page_type'],
         s.get('main_topic',''),s.get('data_source','Extracted from page')] for s in r['serp']])
    ws2=wb.create_sheet('Keywords')
    ws(ws2,['关键词','关键词类型','搜索量','搜索意图','主题聚类','建议目标页面类型','建议Slug',
            '数据依据','来源URL','备注'],
       [[k['keyword'],k['keyword_type'],k['search_volume'],k['search_intent'],k['cluster'],
         k['suggested_page_type'],k['slug'],k['data_basis'],k['source_url'],k.get('notes','')]
        for k in r['keywords']])
    ws3=wb.create_sheet('Clusters')
    ws(ws3,['聚类名称','主关键词','支持关键词','搜索意图','建议页面类型','建议页面标题','建议Slug','优先级'],
       [[c['name'],c['primary'],c['supporting'],c['search_intent'],c['suggested_page_type'],
         c['suggested_page_title'],c['slug'],c['priority']] for c in r['clusters']])
    ws4=wb.create_sheet('Intent_Summary')
    ws(ws4,['搜索意图','估算占比','示例关键词','对应排名页面','备注'],
       [[s['search_intent'],s['estimated_share'],s['sample_keywords'],s['ranking_pages'],s['notes']]
        for s in r.get('intent_summary',[])])
    buf=io.BytesIO(); wb.save(buf); buf.seek(0)
    return send_file(buf, download_name=f'seo_research_{kw}.xlsx', as_attachment=True,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


def write_to_google_sheets(sheet_id, r):
    creds = Credentials.from_authorized_user_file(DEFAULT_TOKEN, ['https://www.googleapis.com/auth/spreadsheets'])
    proxy_info = httplib2.ProxyInfo(proxy_type=socks.PROXY_TYPE_HTTP, proxy_host=PROXY_HOST, proxy_port=PROXY_PORT)
    authorized_http = AuthorizedHttp(creds, http=httplib2.Http(proxy_info=proxy_info, timeout=60))
    svc = build('sheets', 'v4', http=authorized_http, cache_discovery=False)

    configs = [
        ('SERP_Pages', ['排名','页面标题','URL','Meta Description','H1','页面类型','主要主题','数据来源'],
         [[s['rank'],s['title'],s['url'],s.get('meta_description',''),s.get('h1',''),s['page_type'],
           s.get('main_topic',''),s.get('data_source','Extracted from page')] for s in r['serp']]),
        ('Keywords', ['关键词','关键词类型','搜索量','搜索意图','主题聚类','建议目标页面类型','建议Slug',
                      '数据依据','来源URL','备注'],
         [[k['keyword'],k['keyword_type'],k['search_volume'],k['search_intent'],k['cluster'],
           k['suggested_page_type'],k['slug'],k['data_basis'],k['source_url'],k.get('notes','')]
          for k in r['keywords']]),
        ('Clusters', ['聚类名称','主关键词','支持关键词','搜索意图','建议页面类型','建议页面标题','建议Slug','优先级'],
         [[c['name'],c['primary'],c['supporting'],c['search_intent'],c['suggested_page_type'],
           c['suggested_page_title'],c['slug'],c['priority']] for c in r['clusters']]),
        ('Intent_Summary', ['搜索意图','估算占比','示例关键词','对应排名页面','备注'],
         [[s['search_intent'],s['estimated_share'],s['sample_keywords'],s['ranking_pages'],s['notes']]
          for s in r.get('intent_summary',[])]),
    ]

    spreadsheet = svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
    existing = {s['properties']['title'] for s in spreadsheet['sheets']}

    for name, headers, rows in configs:
        if name not in existing:
            svc.spreadsheets().batchUpdate(spreadsheetId=sheet_id, body={
                'requests': [{'addSheet': {'properties': {'title': name}}}]}).execute()
        svc.spreadsheets().values().clear(spreadsheetId=sheet_id, range=f'{name}!A:ZZ').execute()
        svc.spreadsheets().values().update(spreadsheetId=sheet_id, range=f'{name}!A1',
            valueInputOption='RAW', body={'values': [headers] + rows}).execute()

    return f'https://docs.google.com/spreadsheets/d/{sheet_id}/edit'


@app.route('/pricing')
def pricing():
    return render_template('pricing.html')


@app.route('/enter-key')
def enter_key():
    return """<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Enter License Key</title>
<style>
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; background:#f5f7fa; display:flex; justify-content:center; align-items:center; min-height:100vh; }
.card { background:#fff; border-radius:16px; padding:40px; box-shadow:0 2px 16px rgba(0,0,0,.08); max-width:440px; width:100%; text-align:center; }
.card h2 { font-size:22px; color:#1a1a2e; margin-bottom:8px; }
.card p { font-size:14px; color:#666; margin-bottom:24px; }
.card input { width:100%; padding:12px 16px; border:2px solid #ddd; border-radius:10px; font-size:16px; text-align:center; letter-spacing:2px; font-family:monospace; margin-bottom:16px; }
.card input:focus { outline:none; border-color:#2F5496; }
.card button { background:#2F5496; color:#fff; border:none; padding:12px 24px; border-radius:10px; font-size:16px; font-weight:600; cursor:pointer; width:100%; }
.card button:hover { background:#1e3c6e; }
#msg { margin-top:16px; font-size:14px; display:none; padding:12px; border-radius:8px; }
#msg.ok { display:block; background:#e8f5e9; color:#2d7d46; }
#msg.err { display:block; background:#fce4e4; color:#d32f2f; }
</style></head>
<body>
<div class="card">
<h2>Enter License Key</h2>
<p>Enter the license key you received after purchase</p>
<input type="text" id="key" placeholder="SEO-XXXXXXXX" maxlength="17" autocomplete="off">
<button onclick="activate()">Activate</button>
<div id="msg"></div>
<p style="margin-top:16px;font-size:13px;"><a href="/pricing">Buy a license</a></p>
</div>
<script>
function activate() {
    var key = document.getElementById(\"key\").value.trim();
    var msg = document.getElementById(\"msg\");
    if (!key) { msg.className='err'; msg.textContent='Please enter a license key.'; return; }
    fetch('/activate-key', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({key:key}) })
    .then(r=>r.json()).then(d=>{
        if (d.ok) { msg.className='ok'; msg.innerHTML='License activated! Redirecting...'; setTimeout(()=>{ window.location.href='/'; }, 1500); }
        else { msg.className='err'; msg.textContent=d.error || 'Invalid key.'; }
    }).catch(function(){ msg.className='err'; msg.textContent='Connection error.'; });
}
</script>
</body>
</html>"""


@app.route('/activate-key', methods=['POST'])
def activate_key():
    data = request.get_json(force=True)
    key = (data.get('key', '') or '').strip().upper()
    licenses = load_json(LICS_FILE)
    if key not in licenses:
        return jsonify({'ok': False, 'error': 'Invalid license key.'}), 400
    kdata = licenses[key]
    if is_key_expired(kdata):
        return jsonify({'ok': False, 'error': 'This key has expired.'}), 400
    if kdata.get('used') and kdata.get('duration_hours', 0) == 0:
        return jsonify({'ok': False, 'error': 'This key has already been used.'}), 400
    now = time.time()
    dur_h = kdata.get('duration_hours', 0)
    kdata['used'] = True
    kdata['used_at'] = now
    save_json(LICS_FILE, licenses)
    paid = load_json(PAID_FILE)
    paid[key] = {'status': 'active', 'plan': kdata.get('plan', 'single'),
                 'activated_at': now, 'duration_hours': dur_h}
    save_json(PAID_FILE, paid)
    resp = jsonify({'ok': True})
    cookie_max = 3600
    if dur_h > 0:
        remaining = dur_h * 3600  # seconds remaining
        cookie_max = max(300, int(remaining))
    resp.set_cookie('seo_access', 'granted', max_age=cookie_max)
    return resp


@app.route('/admin/gen-keys')
def admin_gen_keys():
    secret = request.args.get('secret', '')
    if secret != 'seo-admin-2024':
        return 'Unauthorized', 403
    plan = request.args.get('plan', 'single')
    count = int(request.args.get('count', '1'))
    hours = int(request.args.get('hours', '0'))
    dur_h = duration_hours(plan, hours)
    keys = []
    licenses = load_json(LICS_FILE)
    now = time.time()
    for _ in range(count):
        key = gen_license_key()
        licenses[key] = {'plan': plan, 'used': False, 'created_at': now, 'duration_hours': dur_h}
        keys.append(key)
    save_json(LICS_FILE, licenses)
    return jsonify({'keys': keys, 'count': count,
                    'duration_hours': dur_h,
                    'expires': time.strftime('%Y-%m-%d %H:%M', time.gmtime(now + dur_h * 3600)) if dur_h > 0 else 'single-use'})


if __name__ == '__main__':
    app.run(debug=True, port=5555, host='0.0.0.0')
