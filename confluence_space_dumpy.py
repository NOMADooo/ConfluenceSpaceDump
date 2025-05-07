#!/usr/bin/env python3

import os
import argparse
import time
import json
import re # Import re
import threading
import queue
import concurrent.futures
from pathlib import Path
from urllib.parse import urlparse, urljoin, quote
from html import escape # Import escape for HTML text
import hashlib # Import hashlib for slugify fallback

import requests
from bs4 import BeautifulSoup, NavigableString # Import NavigableString
from atlassian import Confluence
# Selenium imports are present but not actively used if use_browser=False or if API is sufficient
# from selenium import webdriver
# from selenium.webdriver.chrome.options import Options
# from selenium.webdriver.chrome.service import Service
# from selenium.webdriver.common.by import By
# from selenium.webdriver.support.ui import WebDriverWait
# from selenium.webdriver.support import expected_conditions as EC
# from webdriver_manager.chrome import ChromeDriverManager
from tqdm import tqdm

try:
    from dateutil import parser as dateutil_parser
except ImportError:
    dateutil_parser = None
    print("Warning: 'python-dateutil' is not installed. Dates may not be formatted correctly.")
    print("Install using: pip install python-dateutil")


def extract_space_info(url):
    parsed_url = urlparse(url)
    path_parts = [p for p in parsed_url.path.split('/') if p]
    base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"
    if 'wiki' in path_parts:
        base_url = urljoin(base_url, '/wiki')
    if 'spaces' in path_parts:
        space_index = path_parts.index('spaces')
        if len(path_parts) > space_index + 1:
            return base_url, path_parts[space_index + 1]
    raise ValueError("Invalid Confluence space URL. Expected format: https://your-site.atlassian.net/wiki/spaces/SPACEKEY")

class ConfluenceScraper:
    def __init__(self, space_url, output_dir, cookies_file=None, cookies_str=None, use_browser=False, max_workers=10): # Default use_browser to False as Selenium part is not fully active
        self.base_url, self.space_key = extract_space_info(space_url)
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.use_browser = use_browser # Keep for potential future use
        self.max_workers = max_workers
        self.thread_lock = threading.Lock()
        self.session = requests.Session()
        cookies = None
        if cookies_file:
            try:
                with open(cookies_file, 'r') as f: cookies = json.load(f)
                print(f"Loaded {len(cookies)} cookies from {cookies_file}")
            except Exception as e: raise ValueError(f"Error loading cookies from file: {e}")
        elif cookies_str:
            try:
                cookies = []
                for cookie_str_item in cookies_str.split(';'):
                    cookie_str_item = cookie_str_item.strip()
                    if '=' in cookie_str_item:
                        name, value = cookie_str_item.split('=', 1)
                        cookies.append({'name': name.strip(), 'value': value.strip(), 'domain': urlparse(self.base_url).netloc, 'path': '/'})
                print(f"Parsed {len(cookies)} cookies from string")
            except Exception as e: raise ValueError(f"Error parsing cookies string: {e}")
        if not cookies: raise ValueError("No cookies provided. Use either --cookies-file or --cookies")
        for cookie in cookies:
            if 'domain' not in cookie: cookie['domain'] = urlparse(self.base_url).netloc
            self.session.cookies.set(name=cookie.get('name'), value=cookie.get('value'), domain=cookie.get('domain'), path=cookie.get('path', '/'))
        self.confluence = Confluence(url=self.base_url, session=self.session, cloud=True)
        self.styles_dir = self.output_dir / "styles"; self.styles_dir.mkdir(exist_ok=True)
        self.images_dir = self.output_dir / "images"; self.images_dir.mkdir(exist_ok=True)
        self.icons_dir = self.images_dir / "icons"; self.icons_dir.mkdir(exist_ok=True, parents=True)
        self.attachments_dir = self.output_dir / "attachments"; self.attachments_dir.mkdir(exist_ok=True)
        self.create_site_css()
        self.pages_info = {}

    def get_attachment_filename(self, attachment_id, attachment_title):
        str_attachment_id = str(attachment_id) if attachment_id is not None else ""
        clean_id = str_attachment_id
        while clean_id.startswith('att') and len(clean_id) > 3 and clean_id[3:].isalnum():
            clean_id = clean_id[3:]
        if not clean_id or not clean_id.isalnum(): 
            clean_id = hashlib.md5(attachment_title.encode('utf-8', 'replace')).hexdigest()[:12]

        original_extension = ''
        if '.' in attachment_title:
            original_extension = attachment_title.rsplit('.', 1)[-1].lower()
        final_extension_to_use = original_extension if original_extension else 'bin'

        html_extensions = ['html', 'htm'] 
        if final_extension_to_use in html_extensions:
            return f"{clean_id}.{final_extension_to_use}.source" 
        else:
            return f"{clean_id}.{final_extension_to_use}"

    def download_attachment(self, page_id, attachment_id, attachment_title, page_attach_dir, forced_filename=None):
        save_as_filename = forced_filename if forced_filename else self.slugify(attachment_title)
        attachment_path = page_attach_dir / save_as_filename
        
        if attachment_path.exists(): return True, attachment_path

        encoded_attachment_title = quote(attachment_title)
        attachment_urls = [
            f"{self.base_url}/download/attachments/{page_id}/{encoded_attachment_title}?version*",
            f"{self.base_url}/download/attachments/{page_id}/{attachment_id}/{encoded_attachment_title}",
            f"{self.base_url.replace('/wiki', '')}/download/attachments/{page_id}/{encoded_attachment_title}"
        ]
        for url_template in attachment_urls:
            urls_to_try = [url_template.replace("?version*", "?api=v2"), url_template.replace("?version*", "")]
            if "?version*" not in url_template: urls_to_try.append(url_template)
            for url in set(urls_to_try):
                try:
                    response = self.session.get(url, timeout=60, allow_redirects=True)
                    if response.status_code == 200:
                        page_attach_dir.mkdir(parents=True, exist_ok=True)
                        with open(attachment_path, 'wb') as f: f.write(response.content)
                        return True, attachment_path
                except Exception:
                    continue
        return False, None

    def process_attachments(self, page_id, attachments_metadata_list):
        if not attachments_metadata_list: return []
        page_attach_dir = self.attachments_dir / page_id
        downloaded_attachments_info = []
        for attachment_meta in attachments_metadata_list:
            att_id = attachment_meta.get('id')
            original_title = attachment_meta.get('title', '')
            if att_id and original_title:
                clean_filename_for_saving = self.get_attachment_filename(att_id, original_title)
                success, path = self.download_attachment(page_id, att_id, original_title, page_attach_dir, forced_filename=clean_filename_for_saving)
                if success:
                    downloaded_attachments_info.append({'id': att_id, 'title': original_title, 'path': str(path), 'filename': clean_filename_for_saving})
        return downloaded_attachments_info

    def process_embedded_images(self, soup, page_id):
        page_attach_dir = self.attachments_dir / page_id
        for img in soup.find_all('img'):
            if img.has_attr('data-linked-resource-id') and img.has_attr('data-linked-resource-default-alias'):
                att_id = img['data-linked-resource-id']
                original_filename = img['data-linked-resource-default-alias']
                if att_id and original_filename:
                    clean_filename_std = self.get_attachment_filename(att_id, original_filename)
                    success, _ = self.download_attachment(page_id, att_id, original_filename, page_attach_dir, forced_filename=clean_filename_std)
                    if success:
                        relative_image_path = f"attachments/{page_id}/{clean_filename_std}"
                        img['src'] = relative_image_path
                        if 'data-image-src' in img.attrs: img['data-image-src'] = relative_image_path
                        attrs_to_remove = [k for k in img.attrs if k.startswith('data-linked-resource-') or k in ['srcset', 'data-base-url', 'data-height', 'data-width', 'data-unresolved-comment-count', 'data-media-id', 'data-media-type']]
                        for attr_name in attrs_to_remove:
                            if attr_name in img.attrs : del img[attr_name]
        return soup

    def transform_layout_tables(self, soup):
        for table_tag in soup.find_all('table', attrs={'data-layout': True}):
            tbody = table_tag.find('tbody')
            container_for_rows = tbody if tbody else table_tag

            column_layout_rows = [child for child in container_for_rows.children if child.name == 'div' and 'columnLayout' in child.get('class', [])]

            if column_layout_rows:
                new_rows_for_tbody = []
                for child in list(container_for_rows.children):
                    if child.name == 'tr':
                        new_rows_for_tbody.append(child.extract())
                    elif child.name == 'div' and 'columnLayout' in child.get('class', []):
                        new_tr = soup.new_tag('tr')
                        cells_in_layout = child.find_all('div', class_='cell', recursive=False)
                        
                        if not cells_in_layout: 
                            inner_content_holder = child.find('div', class_='innerCell') or child
                            new_td = soup.new_tag('td')
                            if len(table_tag.find_all('th')) > 1: 
                                new_td['colspan'] = str(len(table_tag.find_all('th')))
                            for content_child in list(inner_content_holder.contents):
                                new_td.append(content_child.extract())
                            new_tr.append(new_td)
                        else:
                            for cell_div in cells_in_layout:
                                new_td = soup.new_tag('td')
                                if cell_div.has_attr('data-colspan'): new_td['colspan'] = cell_div['data-colspan']
                                if cell_div.has_attr('rowspan'): new_td['rowspan'] = cell_div['rowspan']
                                inner_cell = cell_div.find('div', class_='innerCell')
                                content_source = inner_cell if inner_cell else cell_div
                                for content_child in list(content_source.contents):
                                    new_td.append(content_child.extract())
                                new_tr.append(new_td)
                        new_rows_for_tbody.append(new_tr)
                        child.extract() 
                    elif isinstance(child, NavigableString) and child.strip():
                        new_rows_for_tbody.append(child.extract())
                    elif child.name and child.name not in ['div']:
                         new_rows_for_tbody.append(child.extract())

                container_for_rows.clear() 
                for item in new_rows_for_tbody:
                    container_for_rows.append(item)
                
                if container_for_rows.name == 'table' and not container_for_rows.find('tbody', recursive=False):
                    actual_tbody = soup.new_tag('tbody')
                    for item in list(container_for_rows.contents): 
                        actual_tbody.append(item.extract())
                    container_for_rows.append(actual_tbody)

                if 'data-layout' in table_tag.attrs:
                    del table_tag['data-layout']
                current_classes = table_tag.get('class', [])
                if 'confluenceTable' not in current_classes:
                    table_tag['class'] = current_classes + ['confluenceTable']
        return soup

    def download_page(self, page_url_ref, output_file, page_id):
        try:
            page_content_data = self.confluence.get_page_by_id(page_id, expand='body.view,children.attachment,ancestors,history,version,space')
            main_html_content_str = page_content_data.get('body', {}).get('view', {}).get('value', "")
            page_body_soup = BeautifulSoup(main_html_content_str, 'html.parser')

            title_from_api = page_content_data.get('title', 'Untitled Page') 
            space_name = page_content_data.get('space', {}).get('name', 'Confluence Page')

            creator, last_modifier, modified_date_str = '', '', 'Unknown date'
            if 'history' in page_content_data:
                history = page_content_data['history']
                if history.get('createdBy'): creator = history['createdBy'].get('displayName', '')
                if history.get('lastUpdated'):
                    last_updated = history['lastUpdated']
                    if isinstance(last_updated, dict):
                        if last_updated.get('by'): last_modifier = last_updated['by'].get('displayName', '')
                        modified_date_str = last_updated.get('when', 'Unknown date')
                        if dateutil_parser and modified_date_str != 'Unknown date':
                            try: modified_date_str = dateutil_parser.isoparse(modified_date_str).strftime("%b %d, %Y")
                            except: pass
            
            page_body_soup = self.process_internal_links(page_body_soup, page_id)
            page_body_soup = self.process_embedded_images(page_body_soup, page_id)
            page_body_soup = self.transform_layout_tables(page_body_soup) 
            page_body_soup = self.simplify_classes(page_body_soup)

            doc = BeautifulSoup("", 'html.parser')
            html_tag = doc.new_tag('html'); doc.append(html_tag)
            head = doc.new_tag('head'); html_tag.append(head)
            
            title_tag = doc.new_tag('title'); title_tag.string = title_from_api; head.append(title_tag)

            link_css = doc.new_tag('link', href='styles/site.css', rel='stylesheet', type='text/css'); head.append(link_css)
            meta_charset = doc.new_tag('meta'); meta_charset['http-equiv'] = 'Content-Type'; meta_charset['content'] = 'text/html; charset=UTF-8'; head.append(meta_charset)

            body_tag = doc.new_tag('body', **{'class': ["theme-default", "aui-theme-default"]}); html_tag.append(body_tag)
            div_page = doc.new_tag('div', id='page'); body_tag.append(div_page)
            div_main_panel = doc.new_tag('div', id='main', **{'class': ['aui-page-panel']}); div_page.append(div_main_panel)
            div_main_header = doc.new_tag('div', id='main-header'); div_main_panel.append(div_main_header)
            breadcrumb_section = doc.new_tag('div', id='breadcrumb-section'); div_main_header.append(breadcrumb_section)
            breadcrumbs_ol = doc.new_tag('ol', id='breadcrumbs'); breadcrumb_section.append(breadcrumbs_ol)
            li_first = doc.new_tag('li', **{'class': 'first'}); span_first = doc.new_tag('span'); a_first = doc.new_tag('a', href='index.html'); a_first.string = space_name
            span_first.append(a_first); li_first.append(span_first); breadcrumbs_ol.append(li_first)

            if 'ancestors' in page_content_data:
                for ancestor in page_content_data['ancestors']:
                    anc_id, anc_title_api = ancestor.get('id', ''), ancestor.get('title', '') 
                    if anc_id and anc_title_api:
                        safe_anc_title = self.slugify(anc_title_api)
                        li_anc = doc.new_tag('li'); span_anc = doc.new_tag('span'); a_anc = doc.new_tag('a', href=f"{safe_anc_title}_{anc_id}.html"); a_anc.string = anc_title_api
                        span_anc.append(a_anc); li_anc.append(span_anc); breadcrumbs_ol.append(li_anc)

            title_h1 = doc.new_tag('h1', id='title-heading', **{'class': 'pagetitle'})
            title_span = doc.new_tag('span', id='title-text'); title_span.string = title_from_api 
            title_h1.append(title_span); div_main_header.append(title_h1)
            
            content_view_div = doc.new_tag('div', id='content', **{'class': 'view'}); div_main_panel.append(content_view_div)
            metadata_div = doc.new_tag('div', **{'class': 'page-metadata'})
            created_by_span = doc.new_tag('span', **{'class': 'author'}); created_by_span.string = creator
            metadata_div.append(doc.new_string("Created by ")); metadata_div.append(created_by_span)
            if last_modifier and modified_date_str != 'Unknown date':
                modified_by_span = doc.new_tag('span', **{'class': 'editor'}); modified_by_span.string = last_modifier
                metadata_div.append(doc.new_string(f", last modified by ")); metadata_div.append(modified_by_span)
                metadata_div.append(doc.new_string(f" on {modified_date_str}"))
            elif modified_date_str != 'Unknown date':
                 metadata_div.append(doc.new_string(f", last modified on {modified_date_str}"))
            content_view_div.append(metadata_div)
            main_content_div = doc.new_tag('div', id='main-content', **{'class': ['wiki-content', 'group']}); content_view_div.append(main_content_div)

            if page_body_soup and page_body_soup.contents:
                for top_level_tag_from_body in list(page_body_soup.contents):
                    main_content_div.append(top_level_tag_from_body.extract())

            attachments_data = page_content_data.get('children', {}).get('attachment', {}).get('results', [])
            if attachments_data:
                dl_attachments_info = self.process_attachments(page_id, attachments_data)
                if dl_attachments_info:
                    page_section_group = doc.new_tag('div', **{'class': ['pageSection', 'group']}); content_view_div.append(page_section_group)
                    page_section_header = doc.new_tag('div', **{'class': 'pageSectionHeader'}); page_section_group.append(page_section_header)
                    h2_att = doc.new_tag('h2', id='attachments', **{'class': 'pageSectionTitle'}); h2_att.string = "Attachments:"; page_section_header.append(h2_att)
                    greybox_div = doc.new_tag('div', align='left', **{'class': 'greybox'}); page_section_group.append(greybox_div)
                    sorted_att_for_display = sorted(attachments_data, key=lambda x: x.get('title', '').lower())
                    for att_data_item in sorted_att_for_display:
                        att_item_id, item_title = att_data_item.get('id', ''), att_data_item.get('title', '')
                        mime_type = att_data_item.get('metadata', {}).get('mediaType', 'application/octet-stream')
                        std_fname_for_link = next((dli['filename'] for dli in dl_attachments_info if dli['id'] == att_item_id), self.get_attachment_filename(att_item_id, item_title))
                        img_bullet = doc.new_tag('img', src='images/icons/bullet_blue.gif', height='8', width='8', alt=''); greybox_div.append(img_bullet)
                        a_att = doc.new_tag('a', href=f"attachments/{page_id}/{std_fname_for_link}"); a_att.string = item_title
                        greybox_div.append(a_att); greybox_div.append(doc.new_string(f" ({mime_type})")); greybox_div.append(doc.new_tag('br'))

            footer_div = doc.new_tag('div', id='footer', role='contentinfo'); div_page.append(footer_div)
            section_footer = doc.new_tag('section', **{'class': 'footer-body'}); footer_div.append(section_footer)
            p_footer = doc.new_tag('p'); p_footer.string = f"Document generated by Confluence on {time.strftime('%b %d, %Y %H:%M')}"; section_footer.append(p_footer)
            div_footer_logo = doc.new_tag('div', id='footer-logo'); a_footer_logo = doc.new_tag('a', href='http://www.atlassian.com/'); a_footer_logo.string = "Atlassian"
            div_footer_logo.append(a_footer_logo); section_footer.append(div_footer_logo)

            final_html_str = "<!DOCTYPE html>\n" + doc.prettify(formatter=None)
            with open(output_file, 'w', encoding='utf-8') as f: f.write(final_html_str)
            return True
        except Exception as e:
            print(f"Error downloading page {page_url_ref} (ID: {page_id}): {e}")
            import traceback; traceback.print_exc()
            return False

    def simplify_classes(self, soup):
        # --- Panel Processing ---
        panel_selectors = 'div.confluence-information-macro, div.ak-editor-panel'
        panels = soup.select(panel_selectors)

        for panel in panels:
            panel_type = None
            content_html = None
            original_attrs = dict(panel.attrs) 

            if panel.has_attr('class') and 'confluence-information-macro' in panel['class']:
                type_classes = {
                    'confluence-information-macro-note': 'note', 'confluence-information-macro-warning': 'warning',
                    'confluence-information-macro-tip': 'tip', 'confluence-information-macro-info': 'info',
                    'confluence-information-macro-information': 'info', 'confluence-information-macro-error': 'error',
                    'confluence-information-macro-success': 'success'}
                current_classes = panel['class']
                for cls, type_name in type_classes.items():
                    if cls in current_classes: panel_type = type_name; break
                body_div = panel.find('div', class_='confluence-information-macro-body', recursive=False)
                if body_div: content_html = body_div.decode_contents()
            elif panel.has_attr('class') and 'ak-editor-panel' in panel['class']:
                panel_type = panel.get('data-panel-type')
                content_div = panel.find('div', class_='ak-editor-panel__content', recursive=False)
                if content_div: content_html = content_div.decode_contents()

            if not panel_type or content_html is None: continue

            new_panel = soup.new_tag('div')
            new_panel['class'] = [f'confluence-information-macro', f'confluence-information-macro-{panel_type}']
            for attr_name, attr_value in original_attrs.items():
                if attr_name.startswith('data-') and attr_name in ['data-panel-type', 'data-local-id']:
                     new_panel[attr_name] = attr_value
            icon_span = soup.new_tag('span')
            icon_class_map = {'note': 'aui-iconfont-warning', 'warning': 'aui-iconfont-error',
                              'info': 'aui-iconfont-info', 'tip': 'aui-iconfont-like',
                              'error': 'aui-iconfont-error', 'success': 'aui-iconfont-approve'}
            icon_class = icon_class_map.get(panel_type, 'aui-iconfont-info')
            icon_span['class'] = ['aui-icon', 'aui-icon-small', icon_class, 'confluence-information-macro-icon']
            new_panel.append(icon_span)
            new_body_div = soup.new_tag('div', **{'class': 'confluence-information-macro-body'})
            parsed_content = BeautifulSoup(content_html, 'html.parser')
            content_nodes = list(parsed_content.body.contents) if parsed_content.body else list(parsed_content.contents)
            for node in content_nodes: new_body_div.append(node.extract())
            new_panel.append(new_body_div)
            panel.replace_with(new_panel)

        # --- Code Panel Processing ---
        for code_panel_div in soup.find_all('div', class_=lambda cl: cl and 'code' in cl and 'panel' in cl):
            is_conf_code_block = any('codeContent' in c_item for child in code_panel_div.children if child.name and hasattr(child, 'has_attr') and child.has_attr('class') for c_item in (child['class'] if isinstance(child['class'], list) else [child['class']]))
            if is_conf_code_block:
                current_classes = code_panel_div['class']
                new_simplified_classes = ['code', 'panel']
                if 'pdl' in current_classes: new_simplified_classes.append('pdl')
                code_panel_div['class'] = new_simplified_classes
                original_attrs = dict(code_panel_div.attrs)
                attrs_to_remove = [k for k in original_attrs if k.startswith('data-') and k not in ['data-syntaxhighlighter-params', 'data-theme']]
                for attr in attrs_to_remove:
                    if attr in code_panel_div.attrs: del code_panel_div[attr]

        # --- Status Macro Cleanup ---
        for status_span in soup.find_all('span', class_='status-macro'):
            allowed_classes = ['status-macro']
            current_classes = status_span.get('class', [])
            if 'aui-lozenge' in current_classes: allowed_classes.append('aui-lozenge')
            type_class = next((cls for cls in current_classes if cls.startswith('aui-lozenge-') and cls != 'aui-lozenge-visual-refresh'), None)
            if type_class: allowed_classes.append(type_class)
            status_span['class'] = allowed_classes
            attrs_to_remove = [k for k in status_span.attrs if k.startswith('data-')]
            for attr in attrs_to_remove:
                if attr in status_span.attrs: del status_span[attr]

        # --- General Data Attribute Cleanup (More Selective) ---
        globally_allowed_data_attributes = {
            'data-syntaxhighlighter-params', 'data-theme', 'data-layout', 
            'data-local-id', 'data-type', 'data-panel-type'}
        for element in soup.find_all(True):
            if hasattr(element, 'attrs'):
                 data_attrs_present = [attr for attr in element.attrs if attr.startswith('data-')]
                 attrs_to_remove = [attr for attr in data_attrs_present if attr not in globally_allowed_data_attributes]
                 for attr_name in attrs_to_remove:
                     if attr_name in element.attrs: del element[attr_name]
        return soup

    def create_site_css(self):
        site_css_path = self.styles_dir / "site.css"
        css_content = """
/** RESET */
html, body, p, div, h1, h2, h3, h4, h5, h6, img, pre, form, fieldset, ul, ol, dl { margin: 0; padding: 0; }
img, fieldset { border: 0; }
body { color: #333; font-family: Arial, sans-serif; font-size: 14px; line-height: 1.5; background-color: #f5f5f5; }
.wiki-content { margin: 10px 0; line-height: 1.5; }
#page { margin: 0 auto; max-width: 1280px; padding: 0 10px; }
#main.aui-page-panel { background-color: #fff; border-radius: 3px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); margin: 20px 0; padding: 20px; }
#main-header { border-bottom: 1px solid #ccc; margin-bottom: 20px; padding-bottom: 10px; }
#breadcrumb-section { font-size: 12px; margin-bottom: 10px; }
#breadcrumbs { list-style: none; padding: 0; }
#breadcrumbs li { display: inline; margin-right: 5px; }
#breadcrumbs li:after { content: " > "; margin-left: 5px; }
#breadcrumbs li.first:before, #breadcrumbs li:last-child:after { content: ""; }
#title-heading.pagetitle { font-size: 24px; font-weight: normal; margin-bottom: 10px; margin-top: 0; }
.page-metadata { color: #707070; font-size: 12px; margin-bottom: 15px; padding-bottom: 10px; border-bottom: 1px solid #eee; }
.page-metadata .author, .page-metadata .editor { font-weight: bold; }
a { color: #3b73af; text-decoration: none; }
a:hover { text-decoration: underline; }
.confluenceTable { border-collapse: collapse; margin: 15px 0; width: 100%; }
.confluenceTh, .confluenceTd { border: 1px solid #ddd; padding: 8px; text-align: left; vertical-align: top; } /* Added vertical-align */
.confluenceTh { background-color: #f5f5f5; font-weight: bold; }
div.code.panel { border: 1px solid #ccc; background-color: #f9f9f9; padding: 0; margin: 1em 0; border-radius: 3px; }
div.codeHeader.panelHeader { padding: 5px 10px; background-color: #f0f0f0; border-bottom: 1px solid #ccc; }
div.codeContent.panelContent { padding: 10px; overflow-x: auto; }
pre.syntaxhighlighter-pre { margin: 0 !important; padding: 0 !important; font-family: monospace; font-size: 13px; line-height: 1.4; background-color: transparent !important; border: none !important; white-space: pre-wrap; word-wrap: break-word; } /* Added wrap */
.confluence-information-macro { border-radius: 3px; border-width: 1px; border-style: solid; padding: 12px; margin: 10px 0; display: flex; align-items: flex-start; }
.confluence-information-macro-icon { margin-right: 8px; flex-shrink: 0; } /* Prevent icon shrinking */
.confluence-information-macro-body { flex-grow: 1; } /* Allow body to take remaining space */
.confluence-information-macro-note { background-color: #e6f2ff; border-color: #b3d9ff; }
.confluence-information-macro-warning { background-color: #ffebe6; border-color: #ffc2b3; }
.confluence-information-macro-info { background-color: #f0f0f0; border-color: #cccccc; }
.confluence-information-macro-tip { background-color: #e6fff2; border-color: #b3ffcc; }
.confluence-information-macro-error { background-color: #ffebe6; border-color: #ffc2b3; } /* Added error style */
.confluence-information-macro-success { background-color: #e6fff2; border-color: #b3ffcc; } /* Added success style */
.aui-icon.aui-icon-small.aui-iconfont-warning, .aui-icon.aui-icon-small.aui-iconfont-error, .aui-icon.aui-icon-small.aui-iconfont-info, .aui-icon.aui-icon-small.aui-iconfont-like, .aui-icon.aui-icon-small.aui-iconfont-approve { display: inline-block; width: 16px; height: 16px; vertical-align: text-bottom; } /* Added more icon classes */
#footer { border-top: 1px solid #ccc; color: #707070; font-size: 12px; margin-top: 30px; padding: 20px 0; text-align: center; }
#footer-logo a { display: inline-block; margin-top: 5px; }
.pageSection.group .pageSectionHeader h2#attachments.pageSectionTitle { font-size: 18px; margin-bottom: 10px; color: #333; }
.greybox { background-color: #f9f9f9; border: 1px solid #eee; padding: 15px; border-radius: 3px; }
.greybox img { vertical-align: middle; margin-right: 5px; }
.greybox br { margin-bottom: 5px; }
.wiki-content img.confluence-embedded-image { max-width: 100%; height: auto; margin: 10px 0; }
.wiki-content span.confluence-embedded-file-wrapper { display: inline-block; margin: 10px 0; }
.wiki-content span.image-center-wrapper { text-align: center; display: block; }
.wiki-content img.image-center { margin-left: auto; margin-right: auto; display: block; }
.wiki-content h1, .wiki-content h2, .wiki-content h3, .wiki-content h4, .wiki-content h5, .wiki-content h6 { margin-top: 1.5em; margin-bottom: 0.5em; font-weight: 600; }
.wiki-content h1 { font-size: 1.8em; } .wiki-content h2 { font-size: 1.6em; } .wiki-content h3 { font-size: 1.4em; }
.wiki-content h4 { font-size: 1.2em; } .wiki-content h5 { font-size: 1.1em; } .wiki-content h6 { font-size: 1em; }
.wiki-content p { margin-bottom: 1em; }
.wiki-content ul, .wiki-content ol { margin-bottom: 1em; padding-left: 30px; }
.wiki-content ul li, .wiki-content ol li { margin-bottom: 0.25em; }
ul, ol { padding-left: 20px; }

/* --- Added block for Confluence Layout Styles --- */
.wiki-content .columnLayout {
    display: table;
    table-layout: fixed; /* 'fixed' helps ensure column widths are respected */
    width: 100%;
    margin-bottom: 8px; /* Consistent spacing */
    box-sizing: border-box;
}
.wiki-content .columnLayout:after { /* Basic clearfix */
    content: "";
    display: table;
    clear: both;
}

.wiki-content .cell {
    display: table-cell;
    vertical-align: top;
    padding: 0 10px; /* Default cell padding */
    box-sizing: border-box;
}

/* Remove padding from first/last cells to align with page edges if they are outer columns */
.wiki-content .columnLayout > .cell:first-child {
    padding-left: 0;
}
.wiki-content .columnLayout > .cell:last-child {
    padding-right: 0;
}

.wiki-content .innerCell {
    overflow-x: auto; /* Crucial for allowing horizontal scroll within cells */
    padding: 1px; /* Prevents margin collapse issues and gives a tiny bit of space */
    box-sizing: border-box;
}
.wiki-content .innerCell > *:first-child {
    margin-top: 0;
}
.wiki-content .innerCell > *:last-child {
    margin-bottom: 0;
}


/* Specific column layout widths */
/* These are based on common Confluence layouts. data-layout attributes in HTML often correspond to these. */

/* Two-column: Left Sidebar (e.g., data-layout="two-left-sidebar") */
.wiki-content .columnLayout.two-left-sidebar .cell.aside {
    width: 29.9%;
}
.wiki-content .columnLayout.two-left-sidebar .cell.normal {
    width: 69.9%; /* Adjust if padding causes overflow, or rely on table-cell auto width */
}

/* Two-column: Right Sidebar (e.g., data-layout="two-right-sidebar") */
.wiki-content .columnLayout.two-right-sidebar .cell.normal {
    width: 69.9%;
}
.wiki-content .columnLayout.two-right-sidebar .cell.aside {
    width: 29.9%;
}

/* Two-column: Equal (e.g., data-layout="two-equal") */
.wiki-content .columnLayout.two-equal .cell {
    width: 50%;
}

/* Three-column: Equal (e.g., data-layout="three-equal") */
.wiki-content .columnLayout.three-equal .cell {
    width: 33.33%;
}

/* Three-column: With Sidebars (e.g., data-layout="three-with-sidebars") */
.wiki-content .columnLayout.three-with-sidebars .cell.aside { /* Assuming 'aside' is used for sidebars */
    width: 24.9%; /* Adjust as needed */
}
.wiki-content .columnLayout.three-with-sidebars .cell.normal {
    width: 49.8%; /* Central column, adjust as needed */
}
/* If three-with-sidebars has specific classes for left/right sidebars, target them */
.wiki-content .columnLayout.three-with-sidebars > .cell:first-child,
.wiki-content .columnLayout.three-with-sidebars > .cell:last-child {
    width: 24.9%; /* Example for sidebars */
}
.wiki-content .columnLayout.three-with-sidebars > .cell:nth-child(2) { /* Middle cell */
    width: 49.8%; /* Example for main content */
}


/* Fixed-width layout (e.g., data-layout="fixed-width") */
/* This usually means the content inside doesn't try to be a column itself, but the .cell takes full width */
.wiki-content .columnLayout.fixed-width .cell {
    width: 100%;
}


/* Table wrapping div */
.wiki-content .table-wrap {
    margin: 10px 0 0 0; /* Consistent with native */
    overflow-x: auto;   /* CRUCIAL for wide tables in narrower columns */
}
.wiki-content .table-wrap:first-child {
    margin-top: 0;
}

/* Ensure .confluenceTable width behaves well within these layouts */
/* .wiki-content .confluenceTable { width: 100%; } is already in your CSS and should work if containers are sized. */
/* You might want to ensure tables don't have excessive default min-width if not needed */
.wiki-content .confluenceTable {
    min-width: initial; /* Or a more specific small value if needed, e.g., 300px */
}
/* --- End of added block --- */
"""
        write_css = True
        if site_css_path.exists():
             try:
                 if site_css_path.read_text(encoding='utf-8') == css_content:
                     write_css = False
             except Exception as e_css_read:
                 print(f"Warning: Could not read existing site.css: {e_css_read}")

        if write_css:
            try:
                with open(site_css_path, 'w', encoding='utf-8') as f: f.write(css_content)
            except Exception as e_css_write:
                 print(f"Error writing site.css: {e_css_write}")


    def scrape_space(self, space_key, skip_existing=False, flat_structure=True):
        self.flat_structure = flat_structure
        print(f"Fetching and filtering page list for space {space_key}...")
        pages_info_list = self.get_all_pages_in_space(space_key)
        if not pages_info_list: print("No current pages found in the space."); return 0

        print(f"Found {len(pages_info_list)} current pages to scrape")
        self.attachments_dir.mkdir(exist_ok=True)
        (self.output_dir / "images" / "icons").mkdir(parents=True, exist_ok=True)
        (self.output_dir / "images" / "icons" / "contenttypes").mkdir(parents=True, exist_ok=True)

        icon_path = self.output_dir / "images" / "icons" / "bullet_blue.gif"
        page_icon_path = self.output_dir / "images" / "icons" / "contenttypes" / "page_16.png"

        if not icon_path.exists():
            try:
                gif_data = bytes.fromhex("47494638396101000100800000000000ffffff21f90401000000002c000000000100010000020144003b")
                with open(icon_path, "wb") as f_icon: f_icon.write(gif_data)
            except Exception as e_icon: print(f"Could not create dummy bullet icon: {e_icon}")
        if not page_icon_path.exists():
             try:
                 png_data = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082")
                 with open(page_icon_path, "wb") as f_icon: f_icon.write(png_data)
             except Exception as e_icon: print(f"Could not create dummy page icon: {e_icon}")


        space_name = space_key
        try:
            space_details = self.confluence.get_space(space_key)
            if space_details and 'name' in space_details: space_name = space_details['name']
        except Exception as e_space: print(f"Could not get space details: {e_space}")

        self.scraped_count, self.failed_count, self.skipped_count = 0, 0, 0
        self.progress_bar = tqdm(total=len(pages_info_list), desc=f"Scraping {space_name} pages", unit="page")

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures_list = []
            for page_info_item in pages_info_list:
                pg_id, pg_title, pg_url = page_info_item['id'], page_info_item['title'], page_info_item['url']
                safe_pg_title = self.slugify(pg_title)
                out_file = self.output_dir / f"{safe_pg_title}_{pg_id}.html"
                if skip_existing and out_file.exists():
                    with self.thread_lock: self.skipped_count += 1; self.progress_bar.update(1)
                    continue
                futures_list.append(executor.submit(self._download_page_task, pg_url, out_file, pg_id, pg_title))
            for future_item in concurrent.futures.as_completed(futures_list):
                try: future_item.result()
                except Exception as exc_future: print(f'Page download task generated an exception: {exc_future}')
        self.progress_bar.close()
        self.create_index_file(space_key, space_name)
        print(f"Scraping completed. {self.scraped_count} pages scraped, {self.skipped_count} skipped, {self.failed_count} failed.")
        return self.scraped_count

    def slugify(self, text):
        if text is None: text = "untitled"
        slug = str(text)
        slug = slug.replace(' - ', '---')
        slug = re.sub(r'[ \t/+&:]', '-', slug)
        slug = re.sub(r'[^\w\-\.]', '', slug, flags=re.UNICODE)
        slug = re.sub(r'-+', '-', slug)
        slug = slug.strip('-_')
        if len(slug) > 100:
            cut_at = slug[:100].rfind('-')
            if cut_at > 50:
                 slug = slug[:cut_at]
            else:
                 slug = slug[:100]
        if not slug:
             slug = hashlib.md5(str(text).encode()).hexdigest()[:10]
        return slug

    def _download_page_task(self, page_url, output_file, page_id, page_title=None):
        success_status = False
        try:
            success_status = self.download_page(page_url, output_file, page_id)
        except Exception as e_task:
            print(f"Unhandled error in download task for page {page_url} (ID: {page_id}): {e_task}")
            import traceback; traceback.print_exc()
        finally:
            with self.thread_lock:
                if success_status: self.scraped_count += 1
                else: self.failed_count += 1
                if hasattr(self, 'progress_bar') and self.progress_bar: self.progress_bar.update(1)
            return success_status

    def get_all_pages_in_space(self, space_key):
        pages_info_list_local = []
        space_name_ref = space_key
        try:
            space_dets = self.confluence.get_space(space_key)
            if space_dets and 'name' in space_dets: space_name_ref = space_dets['name']
        except Exception as e_space_name: print(f"Could not get space name for page list: {e_space_name}")

        print(f"Fetching all pages metadata from space {space_key}...")
        start_idx, batch_limit = 0, 50
        all_pages_api_data = []
        while True:
            try:
                batch_data = self.confluence.get_all_pages_from_space(
                    space=space_key,
                    start=start_idx,
                    limit=batch_limit,
                    status=None,
                    expand="version,ancestors,history,status" 
                )
                if not batch_data:
                    break
                all_pages_api_data.extend(batch_data)
                if len(batch_data) < batch_limit:
                    break
                start_idx += batch_limit
                time.sleep(0.05)
            except Exception as e_batch:
                print(f"Error fetching page batch (start={start_idx}): {e_batch}. Retrying in 3s...")
                time.sleep(3)

        print(f"Processing {len(all_pages_api_data)} fetched page metadata entries...")
        processed_count = 0
        skipped_archived = 0
        for page_api_item in all_pages_api_data:
            pg_item_id = page_api_item.get('id')
            pg_item_title = page_api_item.get('title', 'Untitled')
            pg_item_status = page_api_item.get('status', 'current')

            if pg_item_status != 'current':
                skipped_archived += 1
                continue 

            if pg_item_id:
                processed_count += 1
                webui_link = page_api_item.get('_links', {}).get('webui', f'/spaces/{space_key}/pages/{pg_item_id}')
                pg_item_url = urljoin(self.base_url, webui_link)
                ancestors_data = [{'id': anc_item['id'], 'title': anc_item.get('title', 'Untitled Ancestor')} for anc_item in page_api_item.get('ancestors', []) if anc_item]
                created_by_name = page_api_item.get('history',{}).get('createdBy',{}).get('displayName', 'Unknown')

                page_info_entry = {'id': pg_item_id, 'title': pg_item_title, 'url': pg_item_url, 'ancestors': ancestors_data, 'space_key': space_key, 'space_name': space_name_ref, 'createdBy': created_by_name, 'status': pg_item_status}
                pages_info_list_local.append(page_info_entry)
                self.pages_info[pg_item_id] = page_info_entry 

        print(f"Processed {processed_count} current pages. Skipped {skipped_archived} non-current pages.")
        return pages_info_list_local 


    def create_index_file(self, space_key, space_name):
        def build_page_tree(flat_pages_dict):
            nodes, roots = {pid: {'info': pinfo, 'children': {}} for pid, pinfo in flat_pages_dict.items()}, {}
            for pid, node_item in nodes.items():
                ancestors_list = node_item['info'].get('ancestors', [])
                if ancestors_list:
                    parent_id = str(ancestors_list[-1]['id'])
                    if parent_id in nodes:
                        nodes[parent_id]['children'][pid] = node_item
                    else:
                        roots[pid] = node_item
                else:
                    roots[pid] = node_item
            return roots

        def generate_page_list_html(subtree_dict, level=0):
            html_parts_list = []
            sorted_pids = sorted(subtree_dict.keys(), key=lambda k_pid: subtree_dict[k_pid]['info'].get('title', '').lower())
            
            for page_id_key in sorted_pids:
                node_data = subtree_dict[page_id_key]
                page_info_val = node_data['info']
                
                p_title_original = page_info_val.get('title', 'Untitled') 
                
                safe_p_title_for_filename = self.slugify(p_title_original)
                html_fname_for_href = f"{safe_p_title_for_filename}_{page_id_key}.html" 
                
                display_text_for_link = escape(p_title_original)
                
                indent_str = "    " * (level * 2 + 3)

                html_parts_list.append(f'{indent_str}<li>\n')
                html_parts_list.append(f'{indent_str}    <a href="{escape(html_fname_for_href, quote=True)}">{display_text_for_link}</a>\n')
                html_parts_list.append(f'{indent_str}    <img src="images/icons/contenttypes/page_16.png" height="16" width="16" border="0" align="absmiddle"/>\n')
                
                if node_data['children']:
                    html_parts_list.append(f'{indent_str}    <ul>\n')
                    html_parts_list.append(generate_page_list_html(node_data['children'], level + 1))
                    html_parts_list.append(f'{indent_str}    </ul>\n')
                html_parts_list.append(f'{indent_str}</li>\n')
            return "".join(html_parts_list)

        index_fpath = self.output_dir / "index.html"
        hierarchical_tree = build_page_tree(self.pages_info)
        pages_list_content = generate_page_list_html(hierarchical_tree)

        doc_index = BeautifulSoup("", 'html.parser')
        html_idx_tag = doc_index.new_tag('html'); doc_index.append(html_idx_tag)
        head_idx = doc_index.new_tag('head'); html_idx_tag.append(head_idx)
        
        title_idx = doc_index.new_tag('title'); title_idx.string = f"{space_name} - Space Home"; head_idx.append(title_idx)
        
        link_css_idx = doc_index.new_tag('link', href='styles/site.css', rel='stylesheet', type='text/css'); head_idx.append(link_css_idx)
        meta_idx = doc_index.new_tag('meta'); meta_idx['http-equiv'] = 'Content-Type'; meta_idx['content'] = 'text/html; charset=UTF-8'; head_idx.append(meta_idx)
        
        body_idx_tag = doc_index.new_tag('body', **{'class': ["theme-default", "aui-theme-default"]}); html_idx_tag.append(body_idx_tag)
        div_page_idx = doc_index.new_tag('div', id='page'); body_idx_tag.append(div_page_idx)
        div_main_p_idx = doc_index.new_tag('div', id='main', **{'class': ['aui-page-panel']}); div_page_idx.append(div_main_p_idx)
        div_main_h_idx = doc_index.new_tag('div', id='main-header'); div_main_p_idx.append(div_main_h_idx)
        title_h1_idx = doc_index.new_tag('h1', id='title-heading', **{'class': 'pagetitle'})
        title_span_idx = doc_index.new_tag('span', id='title-text'); title_span_idx.string = "Space Details:"
        title_h1_idx.append(title_span_idx); div_main_h_idx.append(title_h1_idx)
        content_div_idx = doc_index.new_tag('div', id='content'); div_main_p_idx.append(content_div_idx)
        main_content_ps_idx = doc_index.new_tag('div', id='main-content', **{'class': 'pageSection'}); content_div_idx.append(main_content_ps_idx)
        table_dets_idx = doc_index.new_tag('table', **{'class': 'confluenceTable'}); main_content_ps_idx.append(table_dets_idx)
        
        details_map = {"Key": space_key, "Name": space_name, "Description": "", "Created by": ""}
        for key_str, val_str in details_map.items():
            tr = doc_index.new_tag('tr')
            th = doc_index.new_tag('th', **{'class': 'confluenceTh'}); th.string = key_str
            td = doc_index.new_tag('td', **{'class': 'confluenceTd'}); td.string = val_str
            tr.extend([th, td]); table_dets_idx.append(tr)

        content_div_idx.append(doc_index.new_tag('br')); content_div_idx.append(doc_index.new_tag('br'))
        pages_section_div_idx = doc_index.new_tag('div', **{'class': 'pageSection'}); content_div_idx.append(pages_section_div_idx)
        pages_header_div_idx = doc_index.new_tag('div', **{'class': 'pageSectionHeader'}); pages_section_div_idx.append(pages_header_div_idx)
        h2_pages_idx = doc_index.new_tag('h2', **{'class': 'pageSectionTitle'}); h2_pages_idx.string = "Available Pages:"; pages_header_div_idx.append(h2_pages_idx)

        if pages_list_content.strip():
             parsed_list_fragment = BeautifulSoup(f"<ul>\n{pages_list_content}</ul>", 'html.parser')
             ul_pages_soup_idx_tag = parsed_list_fragment.find('ul')
             if ul_pages_soup_idx_tag: pages_section_div_idx.append(ul_pages_soup_idx_tag)

        footer_div_idx = doc_index.new_tag('div', id='footer', role='contentinfo'); div_page_idx.append(footer_div_idx)
        section_footer_idx = doc_index.new_tag('section', **{'class': 'footer-body'}); footer_div_idx.append(section_footer_idx)
        p_footer_idx = doc_index.new_tag('p'); p_footer_idx.string = f"Document generated by Confluence on {time.strftime('%b %d, %Y %H:%M')}"; section_footer_idx.append(p_footer_idx)
        div_footer_logo_idx = doc_index.new_tag('div', id='footer-logo'); a_footer_logo_idx = doc_index.new_tag('a', href='http://www.atlassian.com/'); a_footer_logo_idx.string = "Atlassian"
        div_footer_logo_idx.append(a_footer_logo_idx); section_footer_idx.append(div_footer_logo_idx)

        final_html_idx_str = "<!DOCTYPE html>\n" + doc_index.prettify(formatter=None)
        with open(index_fpath, 'w', encoding='utf-8') as f: f.write(final_html_idx_str)
        print(f"Created index file: {index_fpath}")

    def process_internal_links(self, soup, current_page_id):
        # First pass: Handle Confluence smart links/inline cards
        for smart_link_container in soup.find_all(lambda tag: tag.has_attr('data-card-url')):
            card_url = smart_link_container['data-card-url']
            existing_a_tag = smart_link_container.find('a')
            original_link_text = None
            if existing_a_tag and existing_a_tag.get_text(strip=True):
                original_link_text = existing_a_tag.get_text(strip=True)

            page_id_from_card = None
            parsed_card_url = urlparse(card_url)
            path_segments = [seg for seg in parsed_card_url.path.split('/') if seg]

            if 'pages' in path_segments:
                try:
                    pages_idx = path_segments.index('pages')
                    if len(path_segments) > pages_idx + 1 and path_segments[pages_idx + 1].isdigit():
                        page_id_from_card = path_segments[pages_idx + 1]
                    elif len(path_segments) > pages_idx + 2 and path_segments[pages_idx + 2].isdigit():
                            page_id_from_card = path_segments[pages_idx + 2]
                except ValueError: pass 

            if page_id_from_card and page_id_from_card in self.pages_info:
                linked_page_data = self.pages_info[page_id_from_card]
                safe_linked_title = self.slugify(linked_page_data.get('title', 'Untitled'))
                link_text_to_use = original_link_text if original_link_text else linked_page_data.get('title', 'Untitled Page')
                new_a_tag = soup.new_tag('a', href=f"{safe_linked_title}_{page_id_from_card}.html")
                new_a_tag.string = link_text_to_use
                smart_link_container.replace_with(new_a_tag)

        # Second pass: Process all <a> tags
        for link_el in soup.find_all('a', href=True):
            if not link_el.parent: continue
            href_val = link_el['href']
            
            is_external_or_special = href_val.startswith(('http://', 'https://', '#', 'mailto:'))
            is_local_attachment_link = href_val.startswith('attachments/') or href_val.startswith('../attachments/')

            if is_local_attachment_link or (is_external_or_special and not href_val.startswith(self.base_url)):
                if is_external_or_special and not href_val.startswith(self.base_url) and not link_el.has_attr('rel'):
                    link_el['rel'] = 'nofollow'
                continue

            page_id_from_link = None
            if link_el.has_attr('data-linked-resource-id') and link_el.get('data-linked-resource-type') == 'page':
                page_id_from_link = link_el['data-linked-resource-id']
            
            if not page_id_from_link: 
                parsed_href = urlparse(href_val)
                path_segments = [seg for seg in parsed_href.path.split('/') if seg]
                if 'pages' in path_segments:
                    try:
                        pages_idx = path_segments.index('pages')
                        if len(path_segments) > pages_idx + 1 and path_segments[pages_idx + 1].isdigit():
                            page_id_from_link = path_segments[pages_idx + 1]
                        elif len(path_segments) > pages_idx + 2 and path_segments[pages_idx + 2].isdigit():
                             page_id_from_link = path_segments[pages_idx + 2]
                    except ValueError: pass
                
                if not page_id_from_link and href_val.endswith('.html'):
                    filename_no_ext = href_val.split('/')[-1][:-5]
                    if '_' in filename_no_ext:
                        parts = filename_no_ext.rsplit('_', 1)
                        if len(parts) == 2 and parts[1].isdigit(): page_id_from_link = parts[1]
                    elif filename_no_ext.isdigit(): page_id_from_link = filename_no_ext
                
                if not page_id_from_link and href_val.isdigit() and not '/' in href_val:
                    page_id_from_link = href_val

            if page_id_from_link and page_id_from_link in self.pages_info:
                linked_page_data = self.pages_info[page_id_from_link]
                safe_linked_title = self.slugify(linked_page_data.get('title', 'Untitled'))
                new_href = f"{safe_linked_title}_{page_id_from_link}.html"
                if link_el['href'] != new_href: link_el['href'] = new_href
                
                attrs_to_remove_from_a = [k for k in link_el.attrs if k.startswith('data-linked-resource-') or k.startswith('data-testid') or k == 'tabindex']
                for attr_k in attrs_to_remove_from_a:
                    if attr_k in link_el.attrs: del link_el[attr_k]
                if 'class' in link_el.attrs: del link_el['class']
                if 'rel' in link_el.attrs and link_el['rel'] == 'nofollow': del link_el['rel']
            elif href_val.startswith(('http://', 'https://')) and not link_el.has_attr('rel'):
                link_el['rel'] = 'nofollow'
        return soup

def main():
    parser = argparse.ArgumentParser(description='Scrape Confluence space with fixed attachment handling')
    parser.add_argument('--space-url', required=True, help='URL of the Confluence space')
    parser.add_argument('--output', default='./confluence_output', help='Output directory')
    cookie_group = parser.add_mutually_exclusive_group(required=True)
    cookie_group.add_argument('--cookies-file', help='Path to JSON file containing browser cookies')
    cookie_group.add_argument('--cookies', help='Cookies string "name1=value1; name2=value2"')
    parser.add_argument('--max-workers', type=int, default=5, help='Max worker threads (default 5 for stability).')
    parser.add_argument('--skip-existing', action='store_true', help='Skip existing HTML files.')
    args = parser.parse_args()
    try:
        scraper = ConfluenceScraper(space_url=args.space_url, output_dir=args.output, cookies_file=args.cookies_file, cookies_str=args.cookies, max_workers=args.max_workers)
        scraper.scrape_space(scraper.space_key, skip_existing=args.skip_existing)
    except ValueError as ve: print(f"Config Error: {ve}"); return 1
    except requests.exceptions.RequestException as re: print(f"Request Error: {re}"); return 1
    except Exception as e_main: print(f"Unexpected Main Error: {e_main}"); import traceback; traceback.print_exc(); return 1
    return 0

if __name__ == '__main__':
    exit(main())
