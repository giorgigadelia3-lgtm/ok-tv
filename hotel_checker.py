# hotel_checker.py
# -*- coding: utf-8 -*-

import re
import difflib

# === 1. ტექსტის ნორმალიზაცია (სახელი/მისამართისთვის) ===
def normalize_text(s: str) -> str:
    if not s:
        return ""
    s = s.strip().lower()
    s = re.sub(r"\s+", " ", s)  # ზედმეტი space
    s = re.sub(r"[^\w\u10A0-\u10FF ]+", "", s)  # ვტოვებთ მხოლოდ ასოებს, ციფრებს და ქართულის 지원ს
    return s

# === 2. სიმგავრის დათვლა difflib-ის მიხედვით ===
def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()

# === 3. მთავარი ფუნქცია – ამოწმებს ზუსტ ან მსგავს ჩანაწერს Sheet-ში ===
def check_hotel(name_input: str, addr_input: str, all_hotels: list):
    name_norm = normalize_text(name_input)
    addr_norm = normalize_text(addr_input)

    best_matches = []       # მსგავსი შედეგები
    exact_match = None      # ზუსტი ემთხვევა

    for row in all_hotels:
        sheet_name = normalize_text(str(row.get("hotel name", "")))
        sheet_addr = normalize_text(str(row.get("address", "")))

        # 1) თუ ზუსტად ემთხვევა სახელიც და მისამართიც → ზუსტი შედეგი
        if sheet_name == name_norm and sheet_addr == addr_norm:
            exact_match = row
            break

        # 2) ვიპოვოთ მსგავსი შედეგები (score >= 0.72)
        score = (similarity(sheet_name, name_norm) * 0.6) + (similarity(sheet_addr, addr_norm) * 0.4)
        if score >= 0.72:
            best_matches.append({
                "row": row,
                "score": round(score, 3)
            })

    # დაბრუნება 3 შესაძლო სცენარით:
    if exact_match:
        return {
            "status": "exact",
            "hotel": exact_match
        }
    elif best_matches:
        # დავალაგოთ სიმაგვრით (კლებადობით)
        best_matches = sorted(best_matches, key=lambda x: x["score"], reverse=True)
        return {
            "status": "similar",
            "results": best_matches[:5]   # максимум 5 მსგავსის გამოჩენა
        }
    else:
        return {
            "status": "not_found"
        }
