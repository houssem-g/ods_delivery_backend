"""FR / AR texts of the notifications the order functions send, word for word from the Deno
functions (dispatchOrderToCouriers, cancelOrder, expireStaleOrders, reportOrderIssue)."""

Text = dict[str, str]


def _km(value: float, lang: str = "fr") -> str:
    """« 5,0 » in French (decimal comma, QA B79: « 5.0 km »), « 5.0 » in Arabic, like the app."""
    text = f"{value:.1f}"
    return text if lang == "ar" else text.replace(".", ",")


def minutes_ar(n: int) -> str:
    """« 1 دقيقة », « دقيقتين », « 5 دقائق », « 15 دقيقة »: the Arabic noun agrees with the number."""
    n = int(n)
    if n == 1:
        return "دقيقة واحدة"
    if n == 2:
        return "دقيقتين"
    if 3 <= n <= 10:
        return f"{n} دقائق"
    return f"{n} دقيقة"


def hours_ar(n: int) -> str:
    n = int(n)
    if n == 1:
        return "ساعة واحدة"
    if n == 2:
        return "ساعتين"
    if 3 <= n <= 10:
        return f"{n} ساعات"
    return f"{n} ساعة"


# --- dispatchOrderToCouriers ----------------------------------------------------------------------


def new_order(items: str, shop_name: str | None, distance_km: float) -> Text:
    return {
        "title_ar": "🎯 طلب جديد متاح",
        "title_fr": "🎯 Nouvelle commande disponible",
        "body_ar": f"{items} من {shop_name} - {_km(distance_km, 'ar')} كم",
        "body_fr": f"{items} de {shop_name} - À {_km(distance_km)} km",
    }


def new_order_preferred(client: str, items: str, shop_name: str | None, distance_km: float | None) -> Text:
    dist_fr = "" if distance_km is None else f" - À {_km(distance_km)} km"
    dist_ar = "" if distance_km is None else f" - {_km(distance_km, 'ar')} كم"
    return {
        "title_ar": f"⭐ حريفك {client} قدّم طلباً" if client else "⭐ حريفك قدّم طلباً",
        "title_fr": f"⭐ Votre client {client} a passé une commande"
        if client
        else "⭐ Votre client a passé une commande",
        "body_ar": f"{items} من {shop_name or ''}{dist_ar}",
        "body_fr": f"{items} de {shop_name or ''}{dist_fr}",
    }


# --- cancelOrder --------------------------------------------------------------------------------

REASON_TEXT = {
    "changed_mind": {"fr": "changement d'avis", "ar": "غيّر رأيه"},
    "found_elsewhere": {"fr": "produit trouvé ailleurs", "ar": "وجد المنتج في مكان آخر"},
    "too_expensive": {"fr": "prix trop élevé", "ar": "السعر مرتفع"},
    "taking_too_long": {"fr": "délai trop long", "ar": "التأخير"},
    "product_unavailable": {"fr": "produit indisponible", "ar": "المنتج غير متوفر"},
    "shop_closed": {"fr": "magasin fermé", "ar": "المتجر مغلق"},
    "cannot_reach_customer": {"fr": "client injoignable", "ar": "تعذّر الوصول إلى الحريف"},
    "client_no_response": {"fr": "client injoignable", "ar": "تعذّر الوصول إلى الحريف"},
    "goods_returned_to_shop": {
        "fr": "client injoignable, marchandise rendue au magasin",
        "ar": "تعذّر الوصول إلى الحريف، فأُرجعت البضاعة إلى المتجر",
    },
    "vehicle_issue": {"fr": "problème de véhicule", "ar": "مشكلة في الوسيلة"},
    "emergency": {"fr": "urgence personnelle", "ar": "حالة طوارئ"},
    "other": {"fr": "autre raison", "ar": "سبب آخر"},
    "returned_to_shop": {"fr": "articles rendus au magasin", "ar": "أُرجعت المواد إلى المتجر"},
    "courier_resale": {"fr": "articles revendus en Offre Chaude", "ar": "أُعيد بيع المواد في عرض ساخن"},
}


def reason_text(reason: str, lang: str) -> str:
    known = REASON_TEXT.get(reason)
    return known[lang] if known else str(reason or "")[:120]


def cancelled_by_customer(shop_name: str | None, reason: str) -> Text:
    shop = f" ({shop_name})" if shop_name else ""
    return {
        "title_ar": "❌ تم إلغاء الطلب",
        "title_fr": "❌ Commande annulée",
        "body_ar": f"ألغى الحريف الطلب{shop}: {reason_text(reason, 'ar')}",
        "body_fr": f"Le client a annulé la commande{shop} : {reason_text(reason, 'fr')}",
    }


def offer_closed_by_customer_cancel(shop_name: str | None) -> Text:
    shop = f" ({shop_name})" if shop_name else ""
    return {
        "title_ar": "❌ ألغى الحريف الطلب",
        "title_fr": "❌ Le client a annulé",
        "body_ar": f"ألغى الحريف الطلب{shop}، لم يعد عرضك صالحاً.",
        "body_fr": f"Le client a annulé la commande{shop} : votre offre n'est plus valable.",
    }


def cancelled_by_courier(reason: str, verified_no_response: bool, hot_deal: bool) -> Text:
    if verified_no_response:
        # the courier did nothing wrong: the title doesn't blame him (QA 06/10 B30), same title as
        # after a resale (createHotDeal)
        return {
            "title_ar": "❌ تم إلغاء طلبك",
            "title_fr": "❌ Commande annulée",
            "body_ar": "لم تردّ على المندوب رغم الإشعار والتنبيه، فأُلغي طلبك وسُجّلت حادثة عدم رد.",
            "body_fr": (
                "Vous n'avez pas répondu au livreur malgré la notification et l'alarme : votre commande "
                "est annulée et un incident de non-réponse est enregistré."
            ),
        }
    if hot_deal:
        body_ar = f"السبب: {reason_text(reason, 'ar')}. تم إلغاء الطلب."
        body_fr = f"Raison : {reason_text(reason, 'fr')}. La commande est annulée."
    else:
        body_ar = f"السبب: {reason_text(reason, 'ar')}. طلبك متاح من جديد لبقية المندوبين."
        body_fr = (
            f"Raison : {reason_text(reason, 'fr')}. Votre commande est de nouveau proposée aux livreurs."
        )
    return {
        "title_ar": "⚠️ ألغى المندوب التوصيل",
        "title_fr": "⚠️ Le livreur a annulé",
        "body_ar": body_ar,
        "body_fr": body_fr,
    }


def released_after_block(shop_name: str | None) -> Text:
    """To the courier the customer blocked: the order is taken back (before the purchase)."""
    shop = f" ({shop_name})" if shop_name else ""
    return {
        "title_ar": "❌ أُلغي تكليفك بالطلب",
        "title_fr": "❌ Commande retirée",
        "body_ar": f"اختار الحريف مندوباً آخر لهذا الطلب{shop}. لا تشترِ المواد.",
        "body_fr": (
            f"Le client a choisi un autre livreur pour cette commande{shop}. N'achetez pas les articles."
        ),
    }


def courier_final_cancel(reason: str, shop_name: str | None) -> Text:
    """To the customer: the courier stopped for a reason that ends the order (not sent to others)."""
    shop = f" {shop_name}" if shop_name else ""
    if reason == "shop_closed":
        return {
            "title_ar": "🏪 المتجر مغلق",
            "title_fr": "🏪 Magasin fermé",
            "body_ar": (
                f"وجد المندوب المتجر{shop} مغلقاً، فأُلغي الطلب دون أي مصاريف. يمكنك إعادة الطلب من متجر آخر."
            ),
            "body_fr": (
                f"Le livreur a trouvé le magasin{shop} fermé : la commande est annulée, sans aucun frais. "
                "Vous pouvez la repasser avec un autre magasin."
            ),
        }
    return {
        "title_ar": "⚠️ لم يتمكّن المندوب من إتمام التوصيل",
        "title_fr": "⚠️ Le livreur ne peut pas terminer la livraison",
        "body_ar": "أرجع المندوب المواد للمتجر وأُلغي الطلب. ليس عليك أن تدفع شيئاً.",
        "body_fr": (
            "Le livreur a rendu les articles au magasin et la commande est annulée. Vous n'avez rien à payer."
        ),
    }


# --- expireStaleOrders --------------------------------------------------------------------------


def _label(shop_name: str | None) -> str:
    shop = (shop_name or "").strip()
    return f" ({shop[:40]})" if shop else ""


def expired_open(shop_name: str | None, never_offered: bool, hours: int) -> Text:
    label = _label(shop_name)
    if never_offered:
        body_ar = (
            f"لم يقبل أي مندوب طلبك{label} خلال {hours_ar(hours)}، فأُلغي تلقائياً. "
            "يمكنك إعادة الطلب في أي وقت."
        )
        body_fr = (
            f"Aucun livreur n'a pris votre commande{label} en {hours} h : elle a été annulée "
            "automatiquement. Vous pouvez la repasser à tout moment."
        )
    else:
        body_ar = (
            f"بقي طلبك{label} دون متابعة لمدة {hours_ar(hours)}، فأُلغي تلقائياً وأُغلقت العروض المستلمة. "
            "يمكنك إعادة الطلب في أي وقت."
        )
        body_fr = (
            f"Votre commande{label} est restée sans suite pendant {hours} h : elle a été annulée "
            "automatiquement et les offres reçues sont closes. Vous pouvez la repasser à tout moment."
        )
    return {
        "title_ar": "⌛ انتهت صلاحية طلبك",
        "title_fr": "⌛ Commande expirée",
        "body_ar": body_ar,
        "body_fr": body_fr,
    }


def abandoned(shop_name: str | None, hours: int, for_courier: bool) -> Text:
    label = _label(shop_name)
    body_fr = (
        f"n'a plus bougé depuis plus de {hours} h : elle a été clôturée automatiquement, sans pénalité ni "
        "incident. Contactez le support en cas de question."
    )
    body_ar = (
        f"لم يطرأ عليه أي تحديث منذ أكثر من {hours_ar(hours)}، فأُغلق تلقائياً دون أي عقوبة أو حادثة. "
        "تواصل مع الدعم إن كان لديك سؤال."
    )
    if for_courier:
        return {
            "title_ar": "تم إغلاق التوصيل",
            "title_fr": "Livraison clôturée",
            "body_ar": f"التوصيل{label} {body_ar}",
            "body_fr": f"La livraison{label} {body_fr}",
        }
    return {
        "title_ar": "تم إغلاق الطلب",
        "title_fr": "Commande clôturée",
        "body_ar": f"طلبك{label} {body_ar}",
        "body_fr": f"Votre commande{label} {body_fr}",
    }


# --- reportOrderIssue ---------------------------------------------------------------------------

ISSUE_LABELS = {
    "wrong_address": {"ar": "العنوان خاطئ", "fr": "Mauvaise adresse"},
    "customer_not_available": {"ar": "الحريف غير متاح", "fr": "Client indisponible"},
    "product_damaged": {"ar": "المنتج تالف", "fr": "Produit endommagé"},
    "partial_order": {"ar": "طلب جزئي", "fr": "Commande partielle"},
    "price_different": {"ar": "السعر مختلف", "fr": "Prix différent"},
    "other": {"ar": "مشكلة أخرى", "fr": "Autre problème"},
}


def issue_for_customer(issue_type: str, description: str = "") -> Text:
    """The customer reads the courier's own words too (QA B38: only the type was shown)."""
    label = ISSUE_LABELS.get(issue_type, ISSUE_LABELS["other"])
    words = _short(description, 160) if description else ""
    return {
        "title_ar": "تم الإبلاغ عن مشكلة",
        "title_fr": "Problème signalé",
        "body_ar": f"أبلغ المندوب عن مشكلة: {label['ar']}" + (f" — «{words}»" if words else ""),
        "body_fr": f"Le livreur a signalé un problème : {label['fr']}" + (f" — « {words} »" if words else ""),
    }


def issue_for_admin(issue_type: str, order_ref: str) -> Text:
    label = ISSUE_LABELS.get(issue_type, ISSUE_LABELS["other"])
    return {
        "title_ar": "مشكلة في طلب",
        "title_fr": "Problème de commande",
        "body_ar": f"الطلب #{order_ref}: {label['ar']}",
        "body_fr": f"Commande #{order_ref} : {label['fr']}",
    }


# --- stock checks (reportUnavailableItems / answerStockCheck / timeout) --------------------------


def _price(value: object) -> str:
    return f"{float(value):.3f}" if value is not None else ""  # type: ignore[arg-type]


def _short(text: str | None, limit: int = 80) -> str:
    value = " ".join(str(text or "").split())
    return f"{value[: limit - 1]}…" if len(value) > limit else value


short_text = _short


def stock_check_for_customer(
    missing: str, substitute: str | None, price: object, nothing_available: bool, minutes: int
) -> Text:
    if nothing_available:
        return {
            "title_ar": "🛒 لا يتوفر أي منتج من طلبك في المتجر",
            "title_fr": "🛒 Aucun article de votre commande n'est disponible",
            "body_ar": (
                f"لم يجد المندوب طلبك ({_short(missing)}). أجب خلال {minutes_ar(minutes)}: "
                "ألغِ الطلب دون مصاريف أو اتصل به."
            ),
            "body_fr": (
                f"Le livreur ne trouve pas votre commande ({_short(missing)}). Répondez sous {minutes} min : "
                "annuler sans frais ou l'appeler."
            ),
        }
    if substitute:
        price_ar = f" بسعر {_price(price)} د.ت" if price is not None else ""
        price_fr = f" à {_price(price)} DT" if price is not None else ""
        return {
            "title_ar": "🛒 منتج غير متوفر",
            "title_fr": "🛒 Article indisponible",
            "body_ar": (
                f"{_short(missing)} غير متوفر. يقترح المندوب {_short(substitute, 60)}{price_ar}. "
                f"أجب خلال {minutes_ar(minutes)}."
            ),
            "body_fr": (
                f"{_short(missing)} est indisponible. Le livreur propose {_short(substitute, 60)}{price_fr}. "
                f"Répondez sous {minutes} min."
            ),
        }
    return {
        "title_ar": "🛒 منتج غير متوفر",
        "title_fr": "🛒 Article indisponible",
        "body_ar": (
            f"{_short(missing)} غير متوفر. هل نواصل دونه أم نلغي الطلب؟ أجب خلال {minutes_ar(minutes)}."
        ),
        "body_fr": (
            f"{_short(missing)} est indisponible. Continuer sans ou annuler ? Répondez sous {minutes} min."
        ),
    }


def stock_check_chat(missing: str, substitute: str | None, price: object, nothing_available: bool) -> str:
    """The line the report leaves in the order chat: French on the first line, Arabic on the
    second (one row serves both sides; the app shows only the reader's line, QA 06/10 B51)."""
    if nothing_available:
        return (
            f"🛒 Rien n'est disponible au magasin : {_short(missing, 200)}\n"
            f"🛒 لا يتوفر أي منتج في المتجر: {_short(missing, 200)}"
        )
    fr = f"🛒 Article indisponible : {_short(missing, 200)}"
    ar = f"🛒 منتج غير متوفر: {_short(missing, 200)}"
    if substitute:
        price_fr = f" ({_price(price)} DT)" if price is not None else ""
        price_ar = f" ({_price(price)} د.ت)" if price is not None else ""
        fr += f" · Remplacement proposé : {_short(substitute, 200)}{price_fr}"
        ar += f" · البديل المقترح: {_short(substitute, 200)}{price_ar}"
    return f"{fr}\n{ar}"


DECISION_CHAT = {  # French line, then Arabic line (see stock_check_chat)
    "substitute_accepted": "✅ Remplacement accepté\n✅ تم قبول البديل",
    "item_skipped": "➖ Continuer sans cet article\n➖ مواصلة الطلب دون هذا المنتج",
    "order_cancelled": "❌ Commande annulée (article indisponible)\n❌ تم إلغاء الطلب (منتج غير متوفر)",
}


def stock_decision_for_courier(status: str, decided_by: str, substitute: str | None) -> Text:
    """The customer's (or the policy's) decision, told to the courier."""
    auto_fr = "Sans réponse, le choix fait à la commande s'applique : " if decided_by == "system" else ""
    auto_ar = "لم يُجب الحريف، فطُبّق الاختيار المحدّد عند الطلب: " if decided_by == "system" else ""

    def fr(text: str) -> str:  # a sentence starts with a capital letter (QA 06/10, B80)
        return f"{auto_fr}{text}" if auto_fr else text[:1].upper() + text[1:]

    if status == "substitute_accepted":
        sub = _short(substitute, 60)
        return {
            "title_ar": "✅ قبل الحريف البديل",
            "title_fr": "✅ Remplacement accepté",
            "body_ar": f"{auto_ar}اشترِ البديل ({sub}) وأكمل الطلب.",
            "body_fr": fr(f"achetez le remplacement ({sub}) et continuez la commande."),
        }
    if status == "item_skipped":
        return {
            "title_ar": "➖ أكمل دون المنتج",
            "title_fr": "➖ Continuer sans l'article",
            "body_ar": f"{auto_ar}لا تشترِ المنتج الناقص وأكمل بقية الطلب.",
            "body_fr": fr("n'achetez pas l'article manquant, continuez le reste de la commande."),
        }
    if status == "order_cancelled":
        return {
            "title_ar": "❌ أُلغي الطلب",
            "title_fr": "❌ Commande annulée",
            "body_ar": f"{auto_ar}أُلغي الطلب لأن المنتج غير متوفر. لا عقوبة عليك.",
            "body_fr": fr("la commande est annulée (article indisponible). Aucune pénalité pour vous."),
        }
    # expired: no answer, no automatic decision
    return {
        "title_ar": "⏱️ لم يُجب الحريف",
        "title_fr": "⏱️ Pas de réponse du client",
        "body_ar": "اتصل بالحريف أو ألغِ الطلب دون عقوبة (المنتج غير متوفر).",
        "body_fr": "Pas de réponse — appelez le client ou annulez sans pénalité (article indisponible).",
    }


def stock_timeout_for_customer(status: str, substitute: str | None) -> Text:
    """The deadline passed: what was applied (the customer's own choice at order time)."""
    if status == "substitute_accepted":
        return {
            "title_ar": "✅ طُبّق اختيارك: البديل",
            "title_fr": "✅ Votre choix appliqué : remplacement",
            "body_ar": f"لم تُجب في الوقت المحدّد، فسيشتري المندوب البديل ({_short(substitute, 60)}).",
            "body_fr": (
                f"Sans réponse de votre part, le livreur achète le remplacement ({_short(substitute, 60)})."
            ),
        }
    if status == "item_skipped":
        return {
            "title_ar": "➖ طُبّق اختيارك: دون المنتج",
            "title_fr": "➖ Votre choix appliqué : sans l'article",
            "body_ar": "لم تُجب في الوقت المحدّد، فسيواصل المندوب دون المنتج الناقص.",
            "body_fr": "Sans réponse de votre part, le livreur continue sans l'article manquant.",
        }
    if status == "order_cancelled":
        return {
            "title_ar": "❌ أُلغي طلبك",
            "title_fr": "❌ Commande annulée",
            "body_ar": "المنتج غير متوفر ولم تُجب، فأُلغي الطلب كما اخترت. دون مصاريف.",
            "body_fr": (
                "Article indisponible et pas de réponse : commande annulée comme vous l'aviez choisi. "
                "Sans frais."
            ),
        }
    return {
        "title_ar": "📞 سيتصل بك المندوب",
        "title_fr": "📞 Le livreur va vous appeler",
        "body_ar": "لم تُجب بخصوص المنتج الناقص. سيتصل بك المندوب، أو أجب من التطبيق.",
        "body_fr": (
            "Vous n'avez pas répondu pour l'article manquant. Le livreur va vous appeler, "
            "ou répondez dans l'app."
        ),
    }


def stock_cancel_for_customer() -> Text:
    """The courier cancelled after an unanswered stock check (no penalty for anybody)."""
    return {
        "title_ar": "❌ أُلغي طلبك",
        "title_fr": "❌ Commande annulée",
        "body_ar": "المنتج غير متوفر في المتجر وتعذّر الوصول إليك، فألغى المندوب الطلب. دون مصاريف.",
        "body_fr": (
            "L'article est indisponible et vous n'avez pas pu être joint : le livreur a annulé. Sans frais."
        ),
    }


def stock_check_for_admin(order_ref: str, nothing_available: bool) -> Text:
    label_fr = "Rien n'est disponible" if nothing_available else "Article indisponible"
    label_ar = "لا يتوفر أي منتج" if nothing_available else "منتج غير متوفر"
    return {
        "title_ar": "مشكلة في طلب",
        "title_fr": "Problème de commande",
        "body_ar": f"الطلب #{order_ref}: {label_ar}",
        "body_fr": f"Commande #{order_ref} : {label_fr}",
    }


# --- order steps sent by the server (createOrderOffer, acceptOrderOffer, the courier's steps) ------
# The same words as the app's orderFlow NOTIF_TEXT, which sent them before (still does on the
# installed builds: sendNotificationIfEnabled skips its duplicate).


def short_name(display_name: str | None) -> str:
    """'Karim Trabelsi' → 'Karim T.' (a courier's name shown to customers)."""
    parts = " ".join(str(display_name or "").split()).split(" ")
    if not parts or not parts[0]:
        return ""
    if len(parts) == 1:
        return parts[0][:40]
    return f"{parts[0][:40]} {parts[-1][:1].upper()}."


def _money(value: object) -> str:
    return f"{float(value):.3f}"  # type: ignore[arg-type]


def new_offer_for_customer(courier_name: str | None, fee: object, eta: int | None) -> Text:
    name_fr = short_name(courier_name) or "Un livreur"
    name_ar = short_name(courier_name) or "مندوب"
    eta_fr = f" · ~{eta} min" if eta else ""
    eta_ar = f" · ~{eta} دق" if eta else ""
    return {
        "title_ar": "عرض جديد",
        "title_fr": "Nouvelle offre",
        "body_ar": f"{name_ar} يقترح {_money(fee)} د.ت{eta_ar}",
        "body_fr": f"{name_fr} propose {_money(fee)} DT{eta_fr}",
    }


def offer_accepted_for_courier(shop_name: str | None, items: str | None) -> Text:
    """The title already says « Offre acceptée »: the body goes straight to the shop and the
    articles, never the title again (QA 06/10, R24: « Offre acceptée — Offre acceptée — … »)."""
    shop = _short(shop_name, 60)
    what = _short(items, 80)
    detail_fr = f"{shop} : {what}" if shop and what else (shop or what)
    detail_ar = f"{shop}: {what}" if shop and what else (shop or what)
    return {
        "title_ar": "🎉 تم قبول عرضك",
        "title_fr": "🎉 Offre acceptée",
        "body_ar": f"{detail_ar}. توجّه إلى المتجر." if detail_ar else "توجّه إلى المتجر.",
        "body_fr": f"{detail_fr}. Allez au magasin." if detail_fr else "Allez au magasin.",
    }


def at_shop(shop_name: str | None) -> Text:
    shop = _short(shop_name, 60)
    return {
        "title_ar": "🏪 وصل المندوب إلى المتجر",
        "title_fr": "🏪 Le livreur est au magasin",
        "body_ar": f"المندوب في {shop or 'المتجر'} ويشتري طلبك الآن",
        "body_fr": f"Le livreur est chez {shop or 'le magasin'} et fait vos achats",
    }


def _of_shop(title_fr: str, title_ar: str, shop_name: str | None) -> tuple[str, str]:
    """A step title that names the order's shop: with two orders, the customer knows which one
    moves (QA 06/10, B60: « Le livreur est en route » of another order, right after a block)."""
    shop = _short(shop_name, 40)
    if not shop:
        return title_fr, title_ar
    return f"{title_fr} · {shop}", f"{title_ar} · {shop}"


def purchased(amount: object, shop_name: str | None = None, with_receipt: bool = True) -> Text:
    """« Achat effectué »: the amount is « selon le reçu » only when the courier photographed
    one; without a ticket it is the amount he announced (QA 06/10, N16)."""
    title_fr, title_ar = _of_shop("🛍️ Achat effectué", "🛍️ تم الشراء", shop_name)
    if amount:
        source_fr = "selon le reçu" if with_receipt else "sans ticket, annoncé par le livreur"
        source_ar = "حسب الوصل" if with_receipt else "بدون وصل، حسب ما أعلنه المندوب"
        return {
            "title_ar": title_ar,
            "title_fr": title_fr,
            "body_ar": f"مبلغ المشتريات: {_money(amount)} د.ت ({source_ar})",
            "body_fr": f"Montant des achats : {_money(amount)} DT ({source_fr})",
        }
    return {
        "title_ar": title_ar,
        "title_fr": title_fr,
        "body_ar": "تم شراء طلبك",
        "body_fr": "Votre commande a été achetée",
    }


def on_the_way(eta: int | None, shop_name: str | None = None) -> Text:
    title_fr, title_ar = _of_shop("🚚 Le livreur est en route", "🚚 المندوب في الطريق إليك", shop_name)
    return {
        "title_ar": title_ar,
        "title_fr": title_fr,
        "body_ar": f"الوصول خلال ~{eta} دقيقة. جهّز المبلغ نقداً."
        if eta
        else "المندوب يتجه نحوك الآن. جهّز المبلغ نقداً.",
        "body_fr": f"Arrivée dans ~{eta} min. Préparez le paiement en espèces."
        if eta
        else "Le livreur arrive. Préparez le paiement en espèces.",
    }


def delivered(total: object, shop_name: str | None = None) -> Text:
    title_fr, title_ar = _of_shop("✨ Commande livrée", "✨ تم التوصيل", shop_name)
    if total:
        return {
            "title_ar": title_ar,
            "title_fr": title_fr,
            "body_ar": f"تم توصيل طلبك ({_money(total)} د.ت). قيّم المندوب!",
            "body_fr": f"Votre commande a été livrée ({_money(total)} DT). Notez votre livreur !",
        }
    return {
        "title_ar": title_ar,
        "title_fr": title_fr,
        "body_ar": "تم توصيل طلبك. قيّم المندوب!",
        "body_fr": "Votre commande a été livrée. Notez votre livreur !",
    }
