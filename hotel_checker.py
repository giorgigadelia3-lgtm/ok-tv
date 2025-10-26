# -*- coding: utf-8 -*-
"""
hotel_checker.py
ზედმიწევნით ზუსტი და „მსგავსი“ ძებნა Google Sheets-ში.
- უკიდურესად ტოლერანტული ჰედერებისადმი (მაგ. ' "hotel name ').
- Unicode NFKC ნორმალიზაცია, ზედმეტი სივრცეების შეკვეცა, პუნქტუაციის გაწმენდა.
- სახელისა (EN) და მისამართის (KA) ზუსტი დამთხვევა + ძლიერი მსგავსი ძიება.
Environment:
    SPREADSHEET_ID
    GOOGLE_SERVICE_ACCOUNT_JSON
"""

import os
import re
import json
import unicodedata
import difflib
from typing import List, Dict, Any, Tuple

import gspread
from google.oauth2.service_account import Credentials


# ---------------------------
# ტექსტის ნორმალიზაცია
# ---------------------------
_GEORGIAN_RANGE = r"\u10A0-\u10FF"

def _nfkc(s: str) -> str:
    return unicodedata.normalize("NFKC", s or "")

def _clean_punct_keep_words(s: str) -> str:
    """
    ტოვებს: ლათინურ/ციფრებს/ქართულს და space.
    შლის: ბრჭყალებს, მძიმეებს, სხვ. ნიშნებს.
    """
    s = re.sub(rf"[^\w{_GEORGIAN_RANGE} ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def normalize_strict(s: str) -> str:
    """ სრული ნორმალიზაცია ზუსტი დამთხვევისთვის. """
    s = _nfkc(s).lower().strip()
    # ზოგჯერ ჰედერებში და შიგ ტექსტშიც არის უხილავი სიმბოლოები/ბრჭყალები
    s = s.replace("“", "").replace("”", "").replace('"', "").replace("’", "").replace("'", "")
    s = _clean_punct_keep_words(s)
    return s

def normalize_soft(s: str) -> str:
    """ რბილი გასაღები (similarity) — პუნქტუაციას ნაკლებად ვისჯით. """
    s = _nfkc(s).lower().strip()
    s = re.sub(r"\s+", " ", s)
    return s


# მისამართის მცირე სტანდარტიზაცია (აბრევიატურები/ვარიანტები)
_ADDR_EQUIV = {
    "ქ.": "ქუჩა",
    "ქ ": "ქუჩა ",
    "ქუჩ": "ქუჩა",
    "გამზ.": "გამზირი",
    "აღმაშენებლის გამზ.": "აღმაშენებლის გამზირი",
    "გზატკეცილი": "გზატკეცილი",  # დატოვეთ — უბრალოდ მაგალითი
}

def normalize_address(s: str) -> str:
    s = normalize_strict(s)
    for k, v in _ADDR_EQUIV.items():
        s = s.replace(k, v)
    return s


# ---------------------------
# ჰედერების რუკა (სულაც რომ იყოს „კუთხეში ბრჭყალი“)
# ---------------------------
def _clean_header(h: str) -> str:
    h = normalize_strict(h)
    # ყველაზე მნიშნელავანი სახელები:
    repl = {
        "hotelname": "hotel name",
        "hotel name": "hotel name",
        "სასტუმროს სახელი": "hotel name",

        "address": "address",
        "მისამართი": "address",

        "comment": "comment",
        "კომენტარი": "comment",

        "contact": "contact",
        "საკონტაქტო": "contact",

        "agent": "agent",
        "აგენტ": "agent",

        "name": "name",  # შენთან ეს სვეტი timestamp-ად გამოიყენება
        "თარიღი": "name",
        "timestamp": "name",
        "date": "name",
    }
    return repl.get(h, h)


# ---------------------------
# Google Sheets client
# ---------------------------
class HotelChecker:
    def __init__(self, spreadsheet_id: str = None, service_json: str = None):
        self._spreadsheet_id = spreadsheet_id or os.environ.get("SPREADSHEET_ID")
        self._service_json = service_json or os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
        if not self._spreadsheet_id or not self._service_json:
            raise RuntimeError("SPREADSHEET_ID ან GOOGLE_SERVICE_ACCOUNT_JSON არ არის მითითებული.")

        creds_dict = json.loads(self._service_json)
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        self._client = gspread.authorize(creds)
        sh = self._client.open_by_key(self._spreadsheet_id)
        self._sheet = sh.get_worksheet(0)  # ყოველთვის პირველი worksheet

        self._headers_raw: List[str] = self._sheet.row_values(1)
        self._headers_norm: List[str] = [_clean_header(h) for h in self._headers_raw]
        self._colmap: Dict[str, int] = {name: idx for idx, name in enumerate(self._headers_norm)}

        # ქეში — tuple view: (name_raw, addr_raw, comment_raw, row_dict)
        self._rows: List[Tuple[str, str, str, Dict[str, Any]]] = self._load_rows()

    def _load_rows(self) -> List[Tuple[str, str, str, Dict[str, Any]]]:
        """dict-ებზე დაყრდნობით შეიძლება ქეისები ვერ მოიძებნოს უცნაური ჰედერების გამო.
        ამიტომ ამოვიკითხავთ ველებს ინდექსითაც.
        """
        # სრულად გამოვიყენოთ values, რათა ინდექსით მივწვდეთ ნებისმიერ სვეტს
        values: List[List[str]] = self._sheet.get_all_values()
        rows: List[Tuple[str, str, str, Dict[str, Any]]] = []

        if not values or len(values) < 2:
            return rows

        # dict-ებიც გვინდა (სხვა სვეტებისთვის), მაგრამ name/address/comment — ინდექსით
        # ვიპოვოთ once.
        name_idx = self._colmap.get("hotel name")
        addr_idx = self._colmap.get("address")
        comm_idx = self._colmap.get("comment")

        # header row = values[0]
        for r in range(1, len(values)):
            row_list = values[r]
            # dict map (header -> value) უსაფრთხოდ
            row_dict = {}
            for i, raw_h in enumerate(self._headers_norm):
                val = row_list[i] if i < len(row_list) else ""
                row_dict[raw_h] = val

            name_raw = row_list[name_idx] if name_idx is not None and name_idx < len(row_list) else (row_dict.get("hotel name", "") or "")
            addr_raw = row_list[addr_idx] if addr_idx is not None and addr_idx < len(row_list) else (row_dict.get("address", "") or "")
            comm_raw = row_list[comm_idx] if comm_idx is not None and comm_idx < len(row_list) else (row_dict.get("comment", "") or "")

            # ხანდახან ცარიელი სტრიქონებია ბოლოში — გამოვტოვოთ
            if not (str(name_raw).strip() or str(addr_raw).strip() or str(comm_raw).strip()):
                continue

            rows.append((str(name_raw), str(addr_raw), str(comm_raw), row_dict))

        return rows

    # ---------------------------
    # Public API
    # ---------------------------
    def check(self, input_name_en: str, input_addr_ka: str) -> Dict[str, Any]:
        """
        აბრუნებს:
        {
          "status": "exact" | "similar" | "none",
          "exact_row": dict | None,
          "candidates": [ { "hotel_name": ..., "address": ..., "comment": ..., "score": 0.xx,
                            "score_name": 0.xx, "score_addr": 0.xx } ... ]    # დალაგებული კლებადობით
        }
        """
        name_in = input_name_en or ""
        addr_in = input_addr_ka or ""

        name_in_norm = normalize_strict(name_in)
        addr_in_norm = normalize_address(addr_in)

        # 1) ზუსტი (ორივე ველი)
        for (nm, ad, cm, rowd) in self._rows:
            if normalize_strict(nm) == name_in_norm and normalize_address(ad) == addr_in_norm:
                return {
                    "status": "exact",
                    "exact_row": rowd,
                    "candidates": []
                }

        # 2) მსგავსი — ძლიერი კომბინირებული სკორი
        cands = []
        for (nm, ad, cm, rowd) in self._rows:
            name_sim = difflib.SequenceMatcher(None, normalize_soft(nm), normalize_soft(name_in)).ratio()
            addr_sim = difflib.SequenceMatcher(None, normalize_soft(ad), normalize_soft(addr_in)).ratio()

            # კომბინაცია: სახელზე 0.6, მისამართზე 0.4
            score = round(name_sim * 0.6 + addr_sim * 0.4, 4)

            # კანდიდატად ჩავთვალოთ:
            #   ან კომბინირებული ≥ 0.70
            #   ან ძალიან ძლიერი მსგავსება ერთ-ერთ ველზე (≥ 0.85)
            if score >= 0.70 or name_sim >= 0.85 or addr_sim >= 0.85:
                cands.append({
                    "hotel_name": nm.strip(),
                    "address": ad.strip(),
                    "comment": (cm or "").strip(),
                    "score": score,
                    "score_name": round(name_sim, 4),
                    "score_addr": round(addr_sim, 4),
                })

        cands.sort(key=lambda x: (x["score"], x["score_name"], x["score_addr"]), reverse=True)
        cands = cands[:5]  # ზედმეტი ხმაურისგან

        if cands:
            return {
                "status": "similar",
                "exact_row": None,
                "candidates": cands
            }

        # 3) საერთოდ ვერაფერი
        return {
            "status": "none",
            "exact_row": None,
            "candidates": []
        }


# ---------------------------
# მარტივი helper ფუნქცია იმპორტისთვის
# ---------------------------
_checker_singleton: HotelChecker = None

def get_checker() -> HotelChecker:
    global _checker_singleton
    if _checker_singleton is None:
        _checker_singleton = HotelChecker()
    return _checker_singleton


def check_hotel(input_name_en: str, input_addr_ka: str) -> Dict[str, Any]:
    """
    გარე მოდულებიდან პირდაპირ გამოსაყენებელი ფუნქცია.
    """
    return get_checker().check(input_name_en, input_addr_ka)
