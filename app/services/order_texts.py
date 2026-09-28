"""FR / AR texts of the notifications the order functions send, word for word from the Deno
functions (dispatchOrderToCouriers, cancelOrder, expireStaleOrders, reportOrderIssue)."""

Text = dict[str, str]


def _km(value: float) -> str:
    return f"{value:.1f}"


# --- dispatchOrderToCouriers ----------------------------------------------------------------------


def new_order(items: str, shop_name: str | None, distance_km: float) -> Text:
    return {
        "title_ar": "🎯 طلب جديد متاح",
        "title_fr": "🎯 Nouvelle commande disponible",
        "body_ar": f"{items} من {shop_name} - {_km(distance_km)} كم",
        "body_fr": f"{items} de {shop_name} - À {_km(distance_km)} km",
    }


def new_order_preferred(client: str, items: str, shop_name: str | None, distance_km: float | None) -> Text:
    dist_fr = "" if distance_km is None else f" - À {_km(distance_km)} km"
    dist_ar = "" if distance_km is None else f" - {_km(distance_km)} كم"
    return {
        "title_ar": f"⭐ حريفك {client} عمل طلب" if client else "⭐ حريفك عمل طلب",
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
    "cannot_reach_customer": {"fr": "client injoignable", "ar": "لا يمكن الوصول للعميل"},
    "client_no_response": {"fr": "client injoignable", "ar": "لا يمكن الوصول للعميل"},
    "goods_returned_to_shop": {
        "fr": "client injoignable, marchandise rendue au magasin",
        "ar": "لا يمكن الوصول للعميل، أُرجعت البضاعة للمتجر",
    },
    "vehicle_issue": {"fr": "problème de véhicule", "ar": "مشكلة في الوسيلة"},
    "emergency": {"fr": "urgence personnelle", "ar": "حالة طوارئ"},
    "other": {"fr": "autre raison", "ar": "سبب آخر"},
}


def reason_text(reason: str, lang: str) -> str:
    known = REASON_TEXT.get(reason)
    return known[lang] if known else str(reason or "")[:120]


def cancelled_by_customer(shop_name: str | None, reason: str) -> Text:
    shop = f" ({shop_name})" if shop_name else ""
    return {
        "title_ar": "❌ تم إلغاء الطلب",
        "title_fr": "❌ Commande annulée",
        "body_ar": f"قام العميل بإلغاء الطلب{shop}: {reason_text(reason, 'ar')}",
        "body_fr": f"Le client a annulé la commande{shop} : {reason_text(reason, 'fr')}",
    }


def cancelled_by_courier(reason: str, verified_no_response: bool, hot_deal: bool) -> Text:
    if verified_no_response:
        body_ar = "لم تردّ على المندوب رغم الإشعار ورسائل واتساب/SMS، فأُلغي طلبك وسُجّلت حادثة عدم رد."
        body_fr = (
            "Vous n'avez pas répondu au livreur malgré la notification et WhatsApp/SMS : votre commande "
            "est annulée et un incident de non-réponse est enregistré."
        )
    elif hot_deal:
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


# --- expireStaleOrders --------------------------------------------------------------------------


def _label(shop_name: str | None) -> str:
    shop = (shop_name or "").strip()
    return f" ({shop[:40]})" if shop else ""


def expired_open(shop_name: str | None, never_offered: bool, hours: int) -> Text:
    label = _label(shop_name)
    if never_offered:
        body_ar = (
            f"لم يقبل أي مندوب طلبك{label} خلال {hours} ساعة، فأُلغي تلقائياً. يمكنك إعادة الطلب في أي وقت."
        )
        body_fr = (
            f"Aucun livreur n'a pris votre commande{label} en {hours} h : elle a été annulée "
            "automatiquement. Vous pouvez la repasser à tout moment."
        )
    else:
        body_ar = (
            f"بقي طلبك{label} دون متابعة لمدة {hours} ساعة، فأُلغي تلقائياً وأُغلقت العروض المستلمة. "
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
        f"لم يطرأ عليه أي تحديث منذ أكثر من {hours} ساعة، فأُغلق تلقائياً دون أي عقوبة أو حادثة. "
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
    "customer_not_available": {"ar": "العميل غير متاح", "fr": "Client indisponible"},
    "product_damaged": {"ar": "المنتج تالف", "fr": "Produit endommagé"},
    "partial_order": {"ar": "طلب جزئي", "fr": "Commande partielle"},
    "price_different": {"ar": "السعر مختلف", "fr": "Prix différent"},
    "other": {"ar": "مشكلة أخرى", "fr": "Autre problème"},
}


def issue_for_customer(issue_type: str) -> Text:
    label = ISSUE_LABELS.get(issue_type, ISSUE_LABELS["other"])
    return {
        "title_ar": "تم الإبلاغ عن مشكلة",
        "title_fr": "Problème signalé",
        "body_ar": f"المندوب أبلغ عن مشكلة: {label['ar']}",
        "body_fr": f"Le livreur a signalé un problème: {label['fr']}",
    }


def issue_for_admin(issue_type: str, order_ref: str) -> Text:
    label = ISSUE_LABELS.get(issue_type, ISSUE_LABELS["other"])
    return {
        "title_ar": "مشكلة في طلب",
        "title_fr": "Problème de commande",
        "body_ar": f"الطلب #{order_ref}: {label['ar']}",
        "body_fr": f"Commande #{order_ref}: {label['fr']}",
    }
