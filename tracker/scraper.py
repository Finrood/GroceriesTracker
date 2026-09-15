import requests
from bs4 import BeautifulSoup
from decimal import Decimal
from datetime import datetime
from zoneinfo import ZoneInfo
import re
import logging
from urllib.parse import urlparse

from .gtin import split_scraped_code

logger = logging.getLogger(__name__)

BRAZIL_TZ = ZoneInfo('America/Sao_Paulo')

class NFCeScraper:
    HEADERS = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    }

    # SSRF Protection: explicit SEFAZ hosts plus a fail-closed suffix rule for
    # any other state's fiscal portal (*.sefaz/fazenda/sef/sefa.<UF>.gov.br).
    ALLOWED_DOMAINS = [
        'sat.sef.sc.gov.br',
        'www.sef.sc.gov.br',
        'nfce.fazenda.pr.gov.br',
        'nfce.fazenda.sp.gov.br',
        'nfce.fazenda.rj.gov.br',
        'nfce.fazenda.mg.gov.br',
        'nfce.sefaz.rs.gov.br',
        # National contingency / portal hosts
        'www.nfe.fazenda.gov.br',
        'dfe-portal.svrs.rs.gov.br',
        'www.svc.fazenda.gov.br',
        'nfce.svrs.rs.gov.br',
    ]

    # 27 federative units: suffix rule covers states not listed above.
    BRAZIL_UFS = frozenset(
        'AC AL AM AP BA CE DF ES GO MA MG MS MT PA PB PE PI PR RJ RN RO RR RS SC SE SP TO'.split()
    )
    FISCAL_SUFFIXES = ('.sefaz.', '.fazenda.', '.sef.', '.sefa.')

    @classmethod
    def _is_allowed_host(cls, hostname):
        if not hostname:
            return False
        host = hostname.lower().rstrip('.')
        # Block IP literals outright (SSRF: cloud metadata, intranet).
        if re.fullmatch(r'\d+\.\d+\.\d+\.\d+', host):
            return False
        if host in cls.ALLOWED_DOMAINS:
            return True
        for domain in cls.ALLOWED_DOMAINS:
            if host.endswith('.' + domain):
                return True
        # Generic fiscal-portal rule: <anything>.sefaz|fazenda|sef|sefa.<UF>.gov.br
        if host.endswith('.gov.br'):
            for suffix in cls.FISCAL_SUFFIXES:
                idx = host.find(suffix)
                if idx > 0:
                    uf_part = host[idx + len(suffix):]
                    uf = uf_part.split('.')[0].upper()
                    if uf in cls.BRAZIL_UFS and uf_part.endswith('.gov.br'):
                        return True
        return False

    @staticmethod
    def clean_number(raw):
        return re.sub(r'\D', '', raw)

    @staticmethod
    def parse_br_decimal(value_str):
        if not value_str: return Decimal('0.00')
        clean_val = value_str.strip().replace('.', '').replace(',', '.')
        clean_val = re.sub(r'[^\d.]', '', clean_val)
        try:
            return Decimal(clean_val)
        except:
            return Decimal('0.00')

    @staticmethod
    def parse_br_datetime(date_str):
        """
        SEFAZ pages print wall-clock time in America/Sao_Paulo with no UTC offset.
        The old code returned a NAIVE datetime, which Django (USE_TZ=True) stored
        as UTC — shifting every receipt and PriceHistory row +3h. Verified
        empirically 2026-09-15: 29 receipts, scrape-vs-issue delta floor 182min,
        zero below 60min. Returns None when no timestamp is parseable so the
        caller can fail loudly instead of saving a receipt with a fabricated date.
        """
        match = re.search(r'(\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2})', date_str or '')
        if not match:
            return None
        naive = datetime.strptime(match.group(1), '%d/%m/%Y %H:%M:%S')
        return naive.replace(tzinfo=BRAZIL_TZ)

    def scrape_url(self, url):
        # SSRF Check (fail-closed: only Brazilian fiscal portals)
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https') or not self._is_allowed_host(parsed.hostname):
            raise ValueError(f"Security: Domain {parsed.hostname} is not allowed for scraping.")

        response = requests.get(url, headers=self.HEADERS, timeout=15)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        full_text = soup.get_text(separator='\n')
        
        data = {
            'store': {
                'name': self._extract_store_name(soup, full_text),
                'cnpj': self.clean_number(self._extract_cnpj(full_text)),
                'city': self._extract_location(full_text, 'city'),
                'neighborhood': self._extract_location(full_text, 'neighborhood'),
                'street': self._extract_location(full_text, 'street'),
            },
            'receipt': {
                'access_key': self._extract_access_key(soup, full_text),
                'issue_date': self.parse_br_datetime(self._extract_date(full_text)),
                'series': self._extract_metadata(full_text).get('series', ''),
                'number': self._extract_metadata(full_text).get('number', ''),
                'total_amount': self.parse_br_decimal(self._extract_total(full_text)),
                'discount': self.parse_br_decimal(self._extract_discount(full_text)),
                'payment_method': self._extract_payment_method(full_text),
                'tax_federal': self._extract_taxes(full_text).get('federal', 0),
                'tax_state': self._extract_taxes(full_text).get('state', 0),
                'tax_municipal': self._extract_taxes(full_text).get('municipal', 0),
                'consumer_cpf': self._extract_cpf(full_text),
            },
            'items': self._parse_items_robust(soup, full_text)
        }

        # Fail loudly rather than persisting a receipt with fabricated/zero
        # financials. The old fallbacks (datetime.now() / total 0.00) silently
        # poisoned analytics: monthly spend under-reported, dates shifted.
        if data['receipt']['issue_date'] is None:
            raise ValueError("Could not parse the receipt issue date (Emissão) from the page.")
        if data['receipt']['total_amount'] <= 0:
            raise ValueError("Could not parse the receipt total (Valor total) from the page.")
        return data

    def _extract_store_name(self, soup, text):
        el = soup.select_one('.txtTopo')
        if el: return el.text.strip()
        match = re.search(r'DOCUMENTO AUXILIAR.*?\n\n(.*?)\nCNPJ', text, re.DOTALL)
        return match.group(1).strip() if match else "Unknown Store"

    def _extract_cnpj(self, text):
        match = re.search(r'CNPJ:\s*([\d./-]+)', text)
        return match.group(1) if match else ""

    def _extract_location(self, text, part):
        # Look for the address block which usually follows the CNPJ line
        lines = text.split('\n')
        addr_line = ""
        for i, line in enumerate(lines):
            if 'CNPJ:' in line and i + 1 < len(lines):
                # The address is usually 1 or 2 lines after CNPJ
                potential = lines[i+1].strip()
                if ',' in potential and not potential.replace('.','').replace('/','').replace('-','').isdigit():
                    addr_line = potential
                    break
                elif i + 2 < len(lines):
                    potential = lines[i+2].strip()
                    if ',' in potential:
                        addr_line = potential
                        break
        
        if addr_line:
            parts = [p.strip() for p in addr_line.split(',')]
            if part == 'city' and len(parts) >= 2:
                # City is usually 'CIDADE - UF'
                return parts[-2].split('-')[0].strip()
            if part == 'neighborhood' and len(parts) >= 3:
                return parts[-3]
            if part == 'street':
                return parts[0]
            return addr_line
        return "Unknown"

    def _extract_date(self, text):
        match = re.search(r'Emissão:\s*(\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2})', text)
        return match.group(1) if match else ""

    def _extract_access_key(self, soup, text):
        el = soup.select_one('.chave')
        if el: return self.clean_number(el.text)
        match = re.search(r'Chave de acesso:\s*([\d\s]{44,})', text)
        return self.clean_number(match.group(1)) if match else ""

    def _extract_total(self, text):
        match = re.search(r'Valor (?:a pagar|total).*?R\$[:\s]*([\d.,]+)', text, re.IGNORECASE)
        return match.group(1) if match else "0"

    def _extract_discount(self, text):
        # Layouts vary: "Descontos R$:", "Desconto: R$ 5,00", "Desconto R$ 5,00".
        match = re.search(r'Descontos?\s*:?\s*R\$\s*:?\s*([\d.,]+)', text, re.IGNORECASE)
        return match.group(1) if match else "0"

    def _extract_payment_method(self, text):
        match = re.search(r'Forma de pagamento:.*?([\w\s]+?)\s*[\d.,]+', text, re.DOTALL)
        if match and match.group(1).strip():
            return match.group(1).strip()
        # Honest fallback: old default 'Cartão' mislabeled PIX/cash as card.
        return "Outros"

    def _extract_metadata(self, text):
        num = re.search(r'Número:\s*(\d+)', text)
        ser = re.search(r'Série:\s*(\d+)', text)
        return {'number': num.group(1) if num else "", 'series': ser.group(1) if ser else ""}

    def _extract_taxes(self, text):
        fed = re.search(r'FEDERAL R\$\s*([\d.,]+)', text, re.IGNORECASE)
        sta = re.search(r'ESTADUAL R\$\s*([\d.,]+)', text, re.IGNORECASE)
        mun = re.search(r'MUNICIPAL R\$\s*([\d.,]+)', text, re.IGNORECASE)
        return {
            'federal': self.parse_br_decimal(fed.group(1)) if fed else Decimal('0'),
            'state': self.parse_br_decimal(sta.group(1)) if sta else Decimal('0'),
            'municipal': self.parse_br_decimal(mun.group(1)) if mun else Decimal('0'),
        }

    def _extract_cpf(self, text):
        match = re.search(r'CPF:\s*([\d.-]+)', text)
        return self.clean_number(match.group(1)) if match else None

    def _parse_items_robust(self, soup, text):
        items = []
        # Strategy 1: Standard Table Parsing (Common in SC SEFAZ)
        rows = soup.select('table#tabResult tr')
        for row in rows:
            cols = row.find_all('td')
            if len(cols) >= 3:
                name_raw = cols[0].text.strip()
                if "Código" not in name_raw and "Descrição" in name_raw: continue
                name_clean = self._clean_product_name(name_raw.split('(Código')[0].strip())
                code_m = re.search(r'Código:\s*(\d+)', name_raw)
                raw_code = code_m.group(1) if code_m else ""
                # GTIN vs PLU split: short store-local PLUs and in-store weigh
                # codes (20-29 prefix) must NOT populate code_gtin, which is
                # the global cross-store key. internal_code always keeps them.
                gtin, internal = split_scraped_code(raw_code)
                items.append({
                    'name': name_clean,
                    'code_gtin': gtin,
                    'internal_code': internal,
                    'ncm': self._extract_row_ncm(row),
                    'quantity': self.parse_br_decimal(cols[1].text),
                    # Guard: unit cell without a 2-letter uppercase run used to
                    # raise AttributeError ('NoneType' has no group) and fail
                    # the entire scrape.
                    'unit_type': (re.search(r'[A-Z]{2}', cols[2].text).group(0) if (cols[2].text and re.search(r'[A-Z]{2}', cols[2].text)) else "UN"),
                    'unit_price': self.parse_br_decimal(cols[3].text) if len(cols) > 3 else Decimal('0'),
                    'total_price': self.parse_br_decimal(cols[4].text) if len(cols) > 4 else Decimal('0'),
                })
        
        # Strategy 2: Regex Fallback (If table parsing fails)
        if not items:
            pattern = re.compile(
                r'(?:^|\n)(?!\s*Consulta|\s*NFCe|\s*Pública)(.+?)\s+\(Código:\s*(\d+)\s*\).*?' 
                r'Qtde\.:\s*([\d.,]+).*?UN:\s*(\w+).*?Unit\.:\s*([\d.,]+).*?Total\s*\n\s*([\d.,]+)',            
                re.DOTALL | re.IGNORECASE | re.MULTILINE
            )
            for m in pattern.finditer(text):
                name = self._clean_product_name(m.group(1).strip())
                gtin, internal = split_scraped_code(m.group(2).strip())
                items.append({
                    'name': name,
                    'code_gtin': gtin,
                    'internal_code': internal, # raw store code, always kept
                    'ncm': self._extract_row_ncm(None, m.group(0)),
                    'quantity': self.parse_br_decimal(m.group(3)),
                    'unit_type': m.group(4).strip(),
                    'unit_price': self.parse_br_decimal(m.group(5)),
                    'total_price': self.parse_br_decimal(m.group(6)),
                })

        # Strategy 3: Div-based parsing (Common in newer layouts)
        if not items:
            # Look for blocks that resemble items
            # This is a heuristic placeholders for a more complex parser if needed
            pass
            
        return self._finalize_items(items)

    def _extract_row_ncm(self, row=None, text=''):
        """Extract the 8-digit NCM tax code for one item row, or ''.

        NCM is the only store-independent classification on the NFCe; some
        SEFAZ layouts render it as its own column, others as an `NCM:` label
        inside expandable item detail. Row-scoped only: never guess from the
        full page text (that would attach one item's NCM to another).
        """
        scope = ''
        if row is not None:
            try:
                scope = row.get_text(separator=' ')
            except Exception:
                scope = ''
        scope = f"{scope} {text or ''}"
        m = re.search(r'NCM\s*[:\-]?\s*(\d{4,8})', scope, re.IGNORECASE)
        if m:
            digits = re.sub(r'\D', '', m.group(1))
            if len(digits) == 8:
                return digits
        return ''

    # NCM chapter (first 2 digits) -> app Category. Used ONLY as a fallback
    # when the keyword guesser returns 'Geral'.
    NCM_CATEGORY_MAP = {
        '02': 'Carnes & Peixes', '03': 'Carnes & Peixes', '16': 'Carnes & Peixes',
        '04': 'Laticínios',
        '07': 'Hortifruti', '08': 'Hortifruti',
        '09': 'Mercearia', '10': 'Mercearia', '11': 'Mercearia',
        '12': 'Mercearia', '14': 'Mercearia', '15': 'Mercearia',
        '17': 'Mercearia', '19': 'Mercearia', '20': 'Mercearia',
        '21': 'Mercearia', '23': 'Pet Shop', '25': 'Mercearia',
        '18': 'Doces & Snacks',
        '22': 'Bebidas',
        '33': 'Higiene', '34': 'Limpeza',
    }

    @classmethod
    def category_for_ncm(cls, ncm):
        digits = re.sub(r'\D', '', ncm or '')
        if len(digits) >= 2:
            return cls.NCM_CATEGORY_MAP.get(digits[:2])
        return None

    def _finalize_items(self, items):
        for item in items:
            item['brand'] = self._guess_brand(item['name'])
            item['category'] = self._guess_category(item['name'])
            if item['category'] == 'Geral' and item.get('ncm'):
                fallback = self.category_for_ncm(item['ncm'])
                if fallback:
                    item['category'] = fallback
            item['normalized_price'] = self._calculate_normalization(item['name'], item['unit_price'], item['unit_type'], item['quantity'])
        return items

    def _guess_category(self, name):
        mapping = {
            'Hortifruti': ['UVA', 'CEBOLA', 'CEBOLINHA', 'TOMATE', 'ABACAXI', 'ABOBORA', 'ABOBRINHA',
                           'BANANA', 'MAMAO', 'PAPAYA', 'PAPAIA', 'BATATA', 'ALHO', 'PIMENTAO', 'LARANJA',
                           'LIMAO', 'CENOURA', 'BETERRABA', 'CHUCHU', 'REPOLHO', 'COUVE',
                           'BROCOLIS', 'BERINJELA', 'QUIABO', 'JILO', 'PEPINO', 'MELANCIA',
                           'MELAO', 'MANGA', 'MACA ', 'PESSEGO', 'MORANGO', 'KIWI', 'ABACATE',
                           'GOIABA', 'MARACUJA', 'CAQUI', 'SALSINHA', 'COENTRO', 'ALFACE',
                           'RUCULA', 'AGRIAO', 'MANDIOCA'],
            'Laticínios': ['IOGURTE', 'IOG', 'QUEIJO', 'LEITE', 'MANTEIGA', 'MARGARINA', 'REQUEIJAO', 'NATAS', 'CREME'],
            'Carnes & Peixes': ['CARNE', 'MOIDA', 'BISTECA', 'COXA', 'FGO', 'FRANGO', 'TILAPIA', 'LING', 'TOSCANA', 'SALSICHA', 'PRESUNTO'],
            'Mercearia': ['ARROZ', 'FEIJAO', 'MACARRAO', 'MAC', 'EXT', 'MOLHO', 'ACUCAR', 'SAL', 'FARINHA', 'OLEO', 'AZEITE', 'OVO'],
            'Bebidas': ['BEB', 'COOLER', 'CERVEJA', 'REFRIGERANTE', 'SUCO', 'AGUA', 'VINO', 'VINHO', 'GUSTO', 'CAFE'],
            'Doces & Snacks': ['CHOC', 'BARRA', 'DOCE', 'AMENDOIM', 'BISCOITO', 'BOLACHA', 'SNACK', 'SALGADINHO'],
            'Limpeza': ['DET', 'LIMPOL', 'ESPONJA', 'SABAO', 'AMACIANTE', 'DESINFETANTE', 'VEJA'],
            'Higiene': ['CD', 'COLGATE', 'DENT', 'CREME DENTAL', 'SABONETE', 'SHAMPOO', 'PAPEL', 'ESMALTE', 'RISQUE'],
            'Pet Shop': ['CAES', 'GATOS', 'ALIM', 'RAÇÃO', 'PEDIGREE', 'WHISKAS'],
        }
        
        name_upper = name.upper()
        for cat, keywords in mapping.items():
            for kw in keywords:
                if kw in name_upper:
                    return cat
        
        return "Geral"

    def _guess_brand(self, name):
        ignore_list = [
            'ARROZ', 'LEITE', 'FEIJAO', 'ACUCAR', 'DET', 'CHOC', 'IOG', 'BEB', 'CR', 'CD', 
            'LIMPOL', 'MAC', 'EXT', 'FILE', 'IOGURTE', 'MANTEIGA', 'BANANA', 'CEBOLA', 
            'TOMATE', 'ABOBORA', 'ALIM', 'OVOS', 'QUEIJO', 'BISTECA', 'CARNE', 'UVA', 'MAMAO',
            'BRANCA', 'LONGA', 'PO', 'BCO', 'UHT', 'PUBLICA', 'INTE', 'ZER', 'ZERO', 'CON', 
            'COND', 'DESN', 'INTEG', 'INT', 'NAT', 'PROMOCAO', 'PESSEG', 'FREE', 'BARRA',
            'CAES', 'DOCE', 'PENSE', 'MOR', 'RALADO', 'LAT', 'UHT', 'LV', 'PG', 'CONSULTA',
            'PUBLICA', 'NFCE', 'DETALHES', 'SC', 'SEMENTE', 'SSEMENTE', 'VIDA', 'COMUM',
            'PEROLA', 'KABOTIA', 'PAPAYA', 'SCOXA', 'KG', 'UN', 'L', 'ML'
        ]
        # Remove common noise words before guessing
        clean_name = self._clean_product_name(name)
        words = re.findall(r'\w+', clean_name.upper())
        if not words: return "Generic"
        
        for word in words:
            clean_word = word.strip()
            if clean_word.isdigit() or clean_word in ignore_list or len(clean_word) < 3:
                continue
            return clean_word.capitalize()
        
        return words[0].capitalize()

    def _clean_product_name(self, name):
        noise = [
            r'Consulta Pública de NFCe',
            r'Pública',
            r'DOCUMENTO AUXILIAR.*?\n',
            r'Aguarde\. Estamos processando.*',
            r'NFCe - Detalhes',
            r'SC\s*\d{2}/\d{2}/\d{4}.*',
            r'\(Código:.*?\)'
        ]
        for n in noise:
            name = re.sub(n, '', name, flags=re.DOTALL | re.IGNORECASE).strip()
        
        if '\n' in name:
            lines = [l.strip() for l in name.split('\n') if l.strip()]
            if lines: name = lines[-1]
            
        return name.strip()

    def _calculate_normalization(self, name, unit_price, unit_type, quantity):
        """
        Normalizes price to 1 unit (1kg, 1L, 1un).
        Handles: "Coca 2L", "Pack 12x350ml", "Arroz 5kg"
        """
        # Ensure we work with Decimals
        unit_price = Decimal(str(unit_price))
        
        # 1. Check for Multipacks explicitly first: "12x350ml", "6 x 1L"
        # Match: (digits) [xX] (digits) (unit)
        multi_match = re.search(r'(\d+)\s*[xX]\s*(\d+(?:[.,]\d+)?)\s*(ML|L|G|KG)', name, re.IGNORECASE)
        if multi_match:
            count = Decimal(multi_match.group(1))
            size = Decimal(multi_match.group(2).replace(',', '.'))
            unit = multi_match.group(3).upper()
            
            # Normalize size to L or KG
            if unit == 'ML' or unit == 'G':
                size = size / Decimal('1000')
            
            total_vol = count * size
            if total_vol > 0:
                # If the receipt says Quantity: 1 (Pack), price is for the pack
                # If receipt says Quantity: 12 (Items), price might be unit...
                # Usually receipt gives Unit Price. If "Pack" is in name, Unit Price is usually for the PACK.
                # So we divide Unit Price by Total Volume.
                return (unit_price / total_vol).quantize(Decimal('0.01'))

        # 2. Check for standard volume/weight in name: "2L", "500g"
        match = re.search(r'(\d+(?:[.,]\d+)?)\s*(KG|G|L|ML)', name, re.IGNORECASE)
        if match:
            val_str = match.group(1).replace(',', '.')
            val = Decimal(val_str)
            unit = match.group(2).upper()
            
            if unit == 'G' or unit == 'ML': 
                val = val / Decimal('1000')
            
            if val > 0:
                return (unit_price / val).quantize(Decimal('0.01'))

        # 3. Fallback to Unit Type from Receipt
        if "KG" in name.upper() or unit_type.upper() == "KG" or unit_type.upper() == "L":
            return unit_price
            
        # 4. If Unit is UN and quantity > 0, maybe it's just price per unit
        return unit_price
