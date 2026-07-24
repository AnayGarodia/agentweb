from __future__ import annotations

import json
import inspect
import re
import time
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import parse_qs, quote, urljoin, urlparse

from bs4 import BeautifulSoup

from agentweb.sdk import (
    AdapterContext,
    AuthenticationRequired,
    HttpSession,
    Response,
    SiteAdapter,
    AgentWebError,
)


BASE_URL = "https://www.amazon.com"
ASIN_PATTERN = re.compile(r"(?<![A-Z0-9])([A-Z0-9]{10})(?![A-Z0-9])", re.I)
SORTS = {
    "featured": None,
    "price_asc": "price-asc-rank",
    "price_desc": "price-desc-rank",
    "reviews": "review-rank",
    "newest": "date-desc-rank",
}
AUTH_COOKIE_NAMES = {"at-main", "sess-at-main", "x-main"}
SEARCH_DEPARTMENTS = {
    "all",
    "arts-crafts",
    "automotive",
    "baby-products",
    "beauty",
    "books",
    "computers",
    "electronics",
    "fashion",
    "garden",
    "grocery",
    "handmade",
    "health-personal-care",
    "industrial",
    "movies-tv",
    "music",
    "office-products",
    "pets",
    "software",
    "sporting",
    "todays-deals",
    "tools",
    "toys-and-games",
    "videogames",
}
DATA_API_ACCEPT = (
    'application/vnd.com.amazon.api+json; type="collection(product/v2)/v1"; '
    'expand="productImages(product.product-images/v2),'
    "buyingOptions[].dealBadge(product.deal-badge/v1),"
    "buyingOptions[].dealDetails(product.deal-details/v1),"
    "buyingOptions[].price(product.price/v1),title(product.offer.title/v1),"
    'buyingOptions[].callToAction(product.call-to-action/v1)"'
)


def clean(value: str | None) -> str | None:
    if value is None:
        return None
    result = " ".join(value.split())
    return result or None


def node_text(node) -> str | None:
    return clean(node.get_text(" ", strip=True)) if node else None


def first_text(root, selectors: list[str]) -> str | None:
    for selector in selectors:
        node = root.select_one(selector)
        value = node_text(node)
        if value:
            return value
    return None


def normalize_rating(value: str | None) -> tuple[str | None, float | None]:
    """Return one stable Amazon rating label and its numeric value."""
    value = clean(value)
    if not value:
        return None, None
    match = re.search(r"([0-5](?:\.[0-9])?)", value)
    if not match:
        return value, None
    number = float(match.group(1))
    return f"{number:.1f} out of 5 stars", number


def parse_review_count(value: str | None) -> int | None:
    """Parse Amazon's count label without confusing prices for counts.

    Handles both grouped integers ("1,234") and the abbreviated form Amazon
    shows on search cards ("1.6K", "(3.2K)", "2.3M") so the numeric field stays
    consistent with the text it was parsed from.
    """
    if not value or "$" in value:
        return None
    match = re.search(r"([0-9][0-9,]*\.?[0-9]*)\s*([KkMmBb])?", value)
    if not match:
        return None
    number = float(match.group(1).replace(",", ""))
    multiplier = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[
        (match.group(2) or "").upper()
    ]
    return int(round(number * multiplier))


def clean_review_body(value: str | None) -> str | None:
    value = clean(value)
    if not value:
        return None
    for phrase in (
        "Brief content visible, double tap to read full content.",
        "Full content visible, double tap to read brief content.",
        "Read more Read less",
        "Read more",
        "Read less",
    ):
        value = value.replace(phrase, " ")
    return clean(value)


def is_product_recommendation(title: str) -> bool:
    promotional = (
        "amazon business card",
        "prime visa",
        "credit card",
        "apply now",
        "amazon store card",
    )
    lowered = title.lower()
    return len(title) >= 4 and not any(term in lowered for term in promotional)


def parse_money(value: str | None) -> float | None:
    if not value:
        return None
    match = re.search(r"(?:US)?\$\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", value)
    if not match:
        return None
    try:
        return float(Decimal(match.group(1).replace(",", "")))
    except InvalidOperation:
        return None


def parse_asin(value: str) -> str:
    match = ASIN_PATTERN.search(value.upper())
    if not match:
        raise AgentWebError(f"Could not find a 10-character ASIN in {value!r}")
    return match.group(1).upper()


def soup_for(response: Response) -> BeautifulSoup:
    soup = BeautifulSoup(response.body, "html.parser")
    text = response.text
    title = node_text(soup.title) or ""
    if (
        "validateCaptcha" in response.url
        or "Enter the characters you see below" in text
        or title.startswith("Robot Check")
        or title.startswith("Sorry!")
    ):
        raise AgentWebError(
            "Amazon returned a CAPTCHA/robot check. Wait and retry, change network, or import a healthy browser session.",
            code="amazon_human_verification_required",
            retryable=True,
            next_action="retry later or run agentweb connect amazon --mode session as a human",
        )
    if "triggerInterstitialChallenge" in text and "bm-verify" in text:
        raise AgentWebError(
            "Amazon's proof-of-work interstitial could not be solved",
            code="amazon_challenge_failed",
            retryable=True,
        )
    if response.status >= 400:
        raise AgentWebError(
            f"Amazon returned HTTP {response.status} for {response.url}"
        )
    return soup


def response_meta(response: Response) -> dict[str, Any]:
    return {
        "elapsed_ms": round(response.elapsed_ms, 1),
        "from_cache": response.from_cache,
        "url": response.url,
        "transport": getattr(response, "transport", "urllib"),
    }


class Adapter(SiteAdapter):
    site_name = "amazon"
    base_url = BASE_URL
    allowed_domains = ("amazon.com",)

    def __init__(self, context: AdapterContext) -> None:
        super().__init__(context)

    def direct_impersonation(self) -> str | None:
        # The login browser uses the matching UA declared by this adapter's
        # manifest. Keep the direct TLS/browser persona aligned with it so
        # Amazon does not reject an otherwise valid imported session.
        return "chrome146"

    def _session_request(self, method: str, url: str, **kwargs: Any) -> Response:
        """Use Chrome impersonation when the installed runtime supports it.

        Adapters are distributed independently from AgentWeb itself. Runtime
        0.12.2's HttpSession predates the impersonate keyword, so passing it
        unconditionally makes every Amazon request fail before reaching the
        network. Keep the newer transport on capable runtimes and fall back to
        the legacy transport on older installations.
        """
        request = self.session().request
        try:
            supports_impersonation = (
                "impersonate" in inspect.signature(request).parameters
            )
        except (TypeError, ValueError):
            supports_impersonation = False
        if supports_impersonation:
            kwargs["impersonate"] = self.direct_impersonation()
        return request(method, url, **kwargs)

    def _has_auth_cookies(self) -> bool:
        return bool(
            {cookie.name for cookie in self.session().cookies} & AUTH_COOKIE_NAMES
        )

    def _cart_scope(self) -> str:
        return "account" if self._has_auth_cookies() else "anonymous"

    def _validate_cart_scope(self, requested: str) -> str:
        if requested not in {"account", "anonymous"}:
            raise AgentWebError("cart_scope must be account or anonymous")
        has_auth_cookies = self._has_auth_cookies()
        if requested == "account" and not has_auth_cookies:
            raise AuthenticationRequired(
                "Adding to 'my Amazon cart' requires signing in once. The public search remains available without login."
            )
        if has_auth_cookies:
            status = self.account_status()
            if requested == "account" and not status["signed_in"]:
                raise AuthenticationRequired(
                    "The saved Amazon session has expired. Sign in once, then retry the cart operation."
                )
            if requested == "anonymous" and status["signed_in"]:
                raise AgentWebError(
                    "This AgentWeb profile is signed in, so its cart belongs to the Amazon account. Use cart_scope=account or a separate unsigned AgentWeb profile."
                )
        return requested

    @staticmethod
    def _is_interstitial(response: Response) -> bool:
        return (
            response.status == 200
            and "triggerInterstitialChallenge" in response.text
            and "bm-verify" in response.text
        )

    def _solve_interstitial(self, response: Response) -> None:
        text = response.text
        token_match = re.search(r'"bm-verify"\s*:\s*"([^"]+)"', text)
        integer_match = re.search(r"var\s+i\s*=\s*([0-9]+)", text)
        number_match = re.search(r'Number\("([0-9]+)"\s*\+\s*"([0-9]+)"\)', text)
        if not token_match or not integer_match or not number_match:
            raise AgentWebError(
                "Amazon changed its proof-of-work interstitial format",
                code="amazon_challenge_changed",
                retryable=True,
            )
        proof = int(integer_match.group(1)) + int(
            number_match.group(1) + number_match.group(2)
        )
        verification = self._session_request(
            "POST",
            BASE_URL + "/_sec/verify?provider=interstitial",
            json_body={"bm-verify": token_match.group(1), "pow": proof},
            referer=response.url,
            headers={"Accept": "application/json"},
        )
        if verification.status >= 400:
            raise AgentWebError(
                f"Amazon rejected its proof-of-work response with HTTP {verification.status}",
                code="amazon_challenge_rejected",
                retryable=True,
            )

    def _request(self, method: str, url: str, **kwargs: Any) -> Response:
        response = self._session_request(method, url, **kwargs)
        if not self._is_interstitial(response):
            return response
        if method.upper() != "GET":
            raise AgentWebError(
                "Amazon challenged a state-changing request; retry later",
                code="amazon_challenge_before_write",
                retryable=True,
            )
        self.session().cache.clear("amazon")
        self._solve_interstitial(response)
        original_fresh = self.session().fresh
        self.session().fresh = True
        try:
            retried = self._session_request(method, url, **kwargs)
        finally:
            self.session().fresh = original_fresh
        if self._is_interstitial(retried):
            raise AgentWebError(
                "Amazon repeated its proof-of-work interstitial after verification",
                code="amazon_challenge_repeated",
                retryable=True,
            )
        return retried

    def _direct_response(self, **kwargs: Any) -> Response:
        response = super()._direct_response(**kwargs)
        if self._is_interstitial(response):
            self._solve_interstitial(response)
            response = super()._direct_response(**kwargs)
        return response

    def search(
        self,
        query: str,
        limit: int = 10,
        page: int = 1,
        sort: str = "featured",
        include_sponsored: bool = False,
        min_price: float | None = None,
        max_price: float | None = None,
        min_rating: float | None = None,
        department: str | None = None,
    ) -> dict[str, Any]:
        if not query.strip():
            raise AgentWebError("Search query cannot be empty")
        if limit < 1 or limit > 50:
            raise AgentWebError("limit must be between 1 and 50")
        if page < 1:
            raise AgentWebError("page must be at least 1")
        if sort not in SORTS:
            raise AgentWebError(f"sort must be one of: {', '.join(SORTS)}")
        if min_price is not None and min_price < 0:
            raise AgentWebError("min_price cannot be negative")
        if max_price is not None and max_price < 0:
            raise AgentWebError("max_price cannot be negative")
        if min_price is not None and max_price is not None and min_price > max_price:
            raise AgentWebError("min_price cannot exceed max_price")
        if min_rating is not None and not 0 <= min_rating <= 5:
            raise AgentWebError("min_rating must be between 0 and 5")
        params: dict[str, Any] = {"k": query, "page": page}
        if department:
            if not re.fullmatch(r"[A-Za-z0-9_-]+", department):
                raise AgentWebError(
                    "department must be an Amazon search index such as electronics"
                )
            if department not in SEARCH_DEPARTMENTS:
                raise AgentWebError(
                    "department is not a supported Amazon search index; omit it for all departments"
                )
            params["i"] = department
        if SORTS[sort]:
            params["s"] = SORTS[sort]
        # Amazon's ascending-price result page can otherwise contain only
        # products below a local minimum. Send numeric price bounds upstream,
        # then retain the local checks below as a correctness guard.
        if min_price is not None:
            params["low-price"] = f"{min_price:g}"
        if max_price is not None:
            params["high-price"] = f"{max_price:g}"
        response = self._request(
            "GET",
            BASE_URL + "/s",
            params=params,
            cache_action="search",
            cache_arguments={
                "query": query,
                "page": page,
                "sort": sort,
                "department": department,
                "min_price": min_price,
                "max_price": max_price,
                "min_rating": min_rating,
                "include_sponsored": include_sponsored,
            },
            cache_ttl=180,
        )
        soup = soup_for(response)
        rows = []
        seen_asins: set[str] = set()
        sponsored_omitted = 0
        filtered_out = 0
        for item in soup.select('[data-component-type="s-search-result"][data-asin]'):
            asin = clean(item.get("data-asin"))
            if not asin or asin in seen_asins:
                continue
            seen_asins.add(asin)
            title = first_text(item, ["h2", "[data-cy=title-recipe] h2"])
            link = item.select_one("h2 a[href]") or item.select_one(
                "a.a-link-normal[href]"
            )
            price_text = first_text(item, [".a-price .a-offscreen"])
            list_text = first_text(
                item,
                [
                    '.a-price.a-text-price[data-a-strike="true"] .a-offscreen',
                    '.a-price[data-a-strike="true"] .a-offscreen',
                ],
            )
            rating_node = item.select_one("[aria-label*='out of 5 stars']")
            rating_raw = (
                clean(rating_node.get("aria-label")) if rating_node else None
            ) or first_text(item, [".a-icon-star-small .a-icon-alt"])
            rating, rating_number = normalize_rating(rating_raw)
            reviews = first_text(item, ["[aria-label$='ratings']", ".s-underline-text"])
            price = parse_money(price_text)
            list_price = parse_money(list_text)
            discount = (
                round((list_price - price) / list_price * 100)
                if price is not None and list_price and price < list_price
                else None
            )
            direct_add = bool(
                item.select_one("button[name='submit.addToCart']")
                or "Add to cart" in item.get_text(" ", strip=True)
            )
            sponsored = "Sponsored" in item.get_text(" ", strip=True)[:250]
            if sponsored and not include_sponsored:
                sponsored_omitted += 1
                continue
            if (
                (min_price is not None and (price is None or price < min_price))
                or (max_price is not None and (price is None or price > max_price))
                or (
                    min_rating is not None
                    and (rating_number is None or rating_number < min_rating)
                )
            ):
                filtered_out += 1
                continue
            rows.append(
                {
                    "asin": asin,
                    "title": title,
                    "price": price,
                    "price_text": price_text,
                    "list_price": list_price,
                    "discount_percent": discount,
                    "rating": rating,
                    "rating_number": rating_number,
                    "review_count": parse_review_count(reviews),
                    "review_count_text": reviews,
                    "sponsored": sponsored,
                    "delivery": first_text(
                        item,
                        ["[data-cy=delivery-recipe]", ".a-color-base.a-text-bold"],
                    ),
                    "can_add_to_cart": True if direct_add else None,
                    "direct_add_from_search": direct_add,
                    "add_to_cart_reason": (
                        None
                        if direct_add
                        else "Not resolved by search. The product may require a variant or offer selection; call amazon.product or attempt amazon.add_to_cart before excluding it."
                    ),
                    "url": f"{BASE_URL}/dp/{asin}",
                }
            )
            if len(rows) >= limit:
                break
        return {
            "operation": "amazon.search",
            "query": query,
            "page": page,
            "sort": sort,
            "department": department,
            "count": len(rows),
            "organic_match_count": sum(not row["sponsored"] for row in rows),
            "sponsored_omitted": sponsored_omitted,
            "filtered_out": filtered_out,
            "filters": {
                "min_price": min_price,
                "max_price": max_price,
                "min_rating": min_rating,
            },
            "no_organic_matches": not any(not row["sponsored"] for row in rows),
            "results": rows,
            "meta": response_meta(response),
        }

    def _product(
        self, asin_value: str, *, cache: bool = True
    ) -> tuple[dict[str, Any], BeautifulSoup, Response]:
        asin = parse_asin(asin_value)
        response = self._request(
            "GET",
            f"{BASE_URL}/dp/{asin}",
            cache_action="product" if cache else None,
            cache_arguments={"asin": asin},
            cache_ttl=300 if cache else 0,
        )
        soup = soup_for(response)
        form = soup.select_one("form#addToCart")
        actual_asin = asin
        if form:
            asin_input = form.select_one("input[name=ASIN]") or form.select_one(
                'input[name="items[0.base][asin]"]'
            )
            if asin_input and asin_input.get("value"):
                actual_asin = asin_input["value"]
        price_text = first_text(
            soup,
            [
                "#corePrice_feature_div .a-price .a-offscreen",
                "#apex_desktop .a-price .a-offscreen",
                "#priceblock_ourprice",
                "#priceblock_dealprice",
            ],
        )
        list_text = first_text(
            soup,
            [
                '#corePrice_feature_div .a-price.a-text-price[data-a-strike="true"] .a-offscreen',
                ".basisPrice .a-offscreen",
            ],
        )
        price = parse_money(price_text)
        list_price = parse_money(list_text)
        displayed_discount = first_text(
            soup, [".savingsPercentage", ".reinventPriceSavingsPercentageMargin"]
        )
        discount_match = re.search(r"([0-9]+)%", displayed_discount or "")
        discount = int(discount_match.group(1)) if discount_match else None
        if discount is None and price is not None and list_price and price < list_price:
            discount = round((list_price - price) / list_price * 100)
        title = first_text(soup, ["#productTitle", "h1"])
        if not title:
            raise AgentWebError(
                f"Amazon product page did not expose product details for {asin}"
            )
        can_add_to_cart = bool(form and form.select_one("[name='submit.add-to-cart']"))
        availability = first_text(soup, ["#availability", "#outOfStock"])
        unavailable = bool(
            availability
            and re.search(
                r"unavailable|out of stock|currently unavailable", availability, re.I
            )
        )
        rating, rating_number = normalize_rating(
            first_text(soup, ["#acrPopover", "#averageCustomerReviews .a-icon-alt"])
        )
        review_count_text = first_text(soup, ["#acrCustomerReviewText"])
        value = {
            "asin": actual_asin,
            "requested_asin": asin,
            "title": title,
            "price": price,
            "price_text": price_text,
            "list_price": list_price,
            "discount_percent": discount,
            "on_sale": bool(
                discount or (price is not None and list_price and price < list_price)
            ),
            "deal_badge": first_text(soup, ["#dealBadgeSupportingText", ".dealBadge"]),
            "rating": rating,
            "rating_number": rating_number,
            "review_count": parse_review_count(review_count_text),
            "review_count_text": review_count_text,
            "availability": availability,
            "seller": first_text(soup, ["#sellerProfileTriggerId", "#merchant-info"]),
            "ships_from": first_text(
                soup,
                [
                    "#fulfillerInfoFeature_feature_div .offer-display-feature-text-message"
                ],
            ),
            "features": [
                node_text(item)
                for item in soup.select("#feature-bullets li")
                if node_text(item)
            ][:12],
            "can_add_to_cart": can_add_to_cart,
            "add_to_cart_reason": (
                None
                if can_add_to_cart
                else "unavailable"
                if unavailable
                else "requires_variant_or_offer_selection"
            ),
            "url": response.url,
        }
        return value, soup, response

    def product(self, asin: str) -> dict[str, Any]:
        value, _, response = self._product(asin)
        return {
            "operation": "amazon.product",
            "product": value,
            "meta": response_meta(response),
        }

    def batch_products(self, asins: list[str]) -> dict[str, Any]:
        """Fetch compact current product data through Amazon's own batch endpoint.

        The browser obtains both short-lived request tokens from the public Deals
        HTML, then calls data.amazon.com. Reproduce that two-request sequence
        directly so an agent does not need a browser or copied credentials.
        """
        if not isinstance(asins, list) or not 1 <= len(asins) <= 25:
            raise AgentWebError("asins must contain between 1 and 25 products")
        parsed = list(dict.fromkeys(parse_asin(value) for value in asins))
        bootstrap = self._request("GET", BASE_URL + "/deals")
        soup = soup_for(bootstrap)
        slate_node = soup.select_one('meta[name="encrypted-slate-token"]')
        slate = clean(slate_node.get("content")) if slate_node else None
        csrf_match = re.search(r'"csrfToken"\s*:\s*"([^"]+)"', bootstrap.text)
        if not slate or not csrf_match:
            raise AgentWebError(
                "Amazon's public Deals page did not expose the batch-data request tokens"
            )
        data_url = (
            "https://data.amazon.com/api/marketplaces/ATVPDKIKX0DER/products/"
            + ",".join(parsed)
        )
        response = self._session_request(
            "GET",
            data_url,
            headers={
                "Accept": DATA_API_ACCEPT,
                "Accept-Language": "en-US",
                "Content-Type": 'application/vnd.com.amazon.api+json; type="product/v2"',
                "Origin": BASE_URL,
                "x-amzn-encrypted-slate-token": slate,
                "x-api-csrf-token": csrf_match.group(1),
            },
            referer=BASE_URL + "/",
        )
        if response.status >= 400:
            raise AgentWebError(
                f"Amazon's batch product endpoint returned HTTP {response.status}; its request-token contract may have changed"
            )
        try:
            payload = json.loads(response.text)
        except json.JSONDecodeError as exc:
            raise AgentWebError(
                "Amazon's batch product endpoint returned invalid JSON"
            ) from exc

        products = []
        returned: set[str] = set()
        for wrapper in payload.get("entities") or []:
            entity = wrapper.get("entity") or {}
            asin = clean(entity.get("asin"))
            if not asin:
                continue
            returned.add(asin)
            title = ((entity.get("title") or {}).get("entity") or {}).get(
                "displayString"
            )
            option = next(
                (
                    item
                    for item in entity.get("buyingOptions") or []
                    if (item.get("price") or {}).get("entity")
                ),
                (entity.get("buyingOptions") or [{}])[0]
                if entity.get("buyingOptions")
                else {},
            )
            price_entity = (option.get("price") or {}).get("entity") or {}
            price = (
                ((price_entity.get("priceToPay") or {}).get("moneyValueOrRange") or {})
                .get("value", {})
                .get("amount")
            )
            list_price = (
                ((price_entity.get("basisPrice") or {}).get("moneyValueOrRange") or {})
                .get("value", {})
                .get("amount")
            )
            discount = (
                (price_entity.get("savings") or {}).get("percentage") or {}
            ).get("value")
            deal_entity = (option.get("dealBadge") or {}).get("entity") or {}
            deal_fragments = (
                (deal_entity.get("messaging") or {}).get("content") or {}
            ).get("fragments") or []
            deal_badge = clean(
                " ".join(str(fragment.get("text") or "") for fragment in deal_fragments)
            )
            action = (option.get("callToAction") or {}).get("entity") or {}
            image_entity = (entity.get("productImages") or {}).get("entity") or {}
            main_image = next(
                (
                    image
                    for image in image_entity.get("images") or []
                    if image.get("variant") == "MAIN"
                ),
                None,
            )
            physical_id = ((main_image or {}).get("hiRes") or {}).get("physicalId") or (
                (main_image or {}).get("lowRes") or {}
            ).get("physicalId")
            products.append(
                {
                    "asin": asin,
                    "title": title,
                    "price": price,
                    "list_price": list_price,
                    "discount_percent": discount,
                    "on_sale": bool(
                        discount or (price and list_price and price < list_price)
                    ),
                    "deal_badge": deal_badge,
                    "can_add_to_cart": "addToCart" in action,
                    "image_url": (
                        f"https://m.media-amazon.com/images/I/{physical_id}._SL1000_.jpg"
                        if physical_id
                        else None
                    ),
                    "url": f"{BASE_URL}/dp/{asin}",
                }
            )
        return {
            "operation": "amazon.batch_products",
            "requested_count": len(parsed),
            "count": len(products),
            "products": products,
            "missing_asins": [asin for asin in parsed if asin not in returned],
            "source": "amazon_data_api_product_v2",
            "browser_required": False,
            "meta": {
                "elapsed_ms": round(bootstrap.elapsed_ms + response.elapsed_ms, 1),
                "request_count": 2,
                "transport": getattr(response, "transport", "urllib"),
            },
        }

    def compare_products(self, asins: list[str]) -> dict[str, Any]:
        if not isinstance(asins, list) or len(asins) < 2 or len(asins) > 10:
            raise AgentWebError("asins must contain between 2 and 10 products")
        products = []
        errors = []
        total_elapsed_ms = 0.0
        for asin_value in asins:
            try:
                value, _, response = self._product(asin_value)
                products.append(value)
                total_elapsed_ms += response.elapsed_ms
            except AgentWebError as exc:
                errors.append({"asin": asin_value, "error": str(exc)})
        priced = [product for product in products if product["price"] is not None]
        rated = []
        for product in products:
            if product.get("rating_number") is not None:
                rated.append((product["rating_number"], product["asin"]))
        return {
            "operation": "amazon.compare_products",
            "requested_count": len(asins),
            "count": len(products),
            "products": products,
            "lowest_price_asin": (
                min(priced, key=lambda product: product["price"])["asin"]
                if priced
                else None
            ),
            "highest_rating_asin": max(rated)[1] if rated else None,
            "errors": errors,
            "meta": {"elapsed_ms": round(total_elapsed_ms, 1)},
        }

    def reviews(
        self,
        asin: str,
        limit: int = 10,
    ) -> dict[str, Any]:
        asin = parse_asin(asin)
        if limit < 1 or limit > 20:
            raise AgentWebError("limit must be between 1 and 20")
        product, soup, response = self._product(asin)
        rows = []
        for review in soup.select('[data-hook="review"]'):
            rating_text = first_text(
                review,
                [
                    '[data-hook="review-star-rating"]',
                    '[data-hook="cmps-review-star-rating"]',
                ],
            )
            rating_match = re.search(r"([0-5](?:\.[0-9])?)", rating_text or "")
            rows.append(
                {
                    "review_id": review.get("id"),
                    "title": first_text(
                        review,
                        ['[data-hook="review-title"]', '[data-hook="reviewTitle"]'],
                    ),
                    "body": clean_review_body(
                        first_text(
                            review,
                            [
                                '[data-hook="reviewRichContentContainer"]',
                                '[data-hook="reviewText"]',
                                '[data-hook="review-body"]',
                            ],
                        )
                    ),
                    "rating": float(rating_match.group(1)) if rating_match else None,
                    "author": first_text(review, [".a-profile-name"]),
                    "date": first_text(review, ['[data-hook="review-date"]']),
                    "verified_purchase": bool(
                        review.select_one('[data-hook="avp-badge"]')
                    ),
                    "helpful_votes": first_text(
                        review, ['[data-hook="helpful-vote-statement"]']
                    ),
                }
            )
            if len(rows) >= limit:
                break
        return {
            "operation": "amazon.reviews",
            "asin": asin,
            "product_title": product["title"],
            "source": "public_product_page_featured_reviews",
            "complete_review_archive": False,
            "note": "Amazon currently redirects the full review archive to sign-in; these are the public featured reviews visible on the product page.",
            "count": len(rows),
            "reviews": rows,
            "meta": response_meta(response),
        }

    def variations(self, asin: str) -> dict[str, Any]:
        product, soup, response = self._product(asin)
        groups = []
        for widget in soup.select('[id^="variation_"]'):
            name = str(widget.get("id", "")).removeprefix("variation_")
            if (
                not name
                or name in {"style_name", "pattern_name"}
                and not widget.select("li, option")
            ):
                continue
            options = []
            for option in widget.select("li[data-defaultasin], option"):
                option_asin = clean(option.get("data-defaultasin"))
                label = clean(option.get("title")) or node_text(option)
                if label:
                    label = re.sub(r"^Click to select\s+", "", label, flags=re.I)
                if not label and not option_asin:
                    continue
                options.append(
                    {
                        "label": label,
                        "asin": option_asin,
                        "selected": "selected" in (option.get("class") or [])
                        or option.has_attr("selected"),
                        "available": "unavailable"
                        not in " ".join(option.get("class") or []).lower(),
                    }
                )
            if options:
                groups.append({"name": name, "options": options[:100]})
        variants = []
        if not groups:

            def script_object(name: str) -> dict[str, Any]:
                match = re.search(
                    rf'"{re.escape(name)}"\s*:\s*(\{{.*?\}})',
                    response.text,
                    re.S,
                )
                if not match:
                    return {}
                try:
                    return json.loads(match.group(1))
                except json.JSONDecodeError:
                    return {}

            variation_values = script_object("variationValues")
            selected_values = script_object("selectedVariationValues")
            display_labels = script_object("variationDisplayLabels")
            dimension_map = script_object("dimensionToAsinMap")
            display_data = script_object("dimensionValuesDisplayData")
            dimensions = list(variation_values)
            for index, name in enumerate(dimensions):
                groups.append(
                    {
                        "name": name,
                        "label": display_labels.get(
                            name, name.replace("_", " ").title()
                        ),
                        "options": [
                            {
                                "label": value,
                                "selected": selected_values.get(name) == value_index,
                            }
                            for value_index, value in enumerate(variation_values[name])
                        ],
                    }
                )
            for combination, variant_asin in dimension_map.items():
                variants.append(
                    {
                        "asin": variant_asin,
                        "selected": variant_asin == product["asin"],
                        "combination_id": combination,
                        "display_values": display_data.get(variant_asin) or [],
                        "url": f"{BASE_URL}/dp/{variant_asin}",
                    }
                )
        return {
            "operation": "amazon.variations",
            "asin": product["asin"],
            "title": product["title"],
            "group_count": len(groups),
            "groups": groups,
            "variant_count": len(variants),
            "variants": variants[:200],
            "meta": response_meta(response),
        }

    def recommendations(self, asin: str, limit: int = 20) -> dict[str, Any]:
        product, soup, response = self._product(asin)
        rows = []
        seen = {product["asin"]}
        promotions_omitted = 0
        for link in soup.select('a[href*="/dp/"]'):
            match = re.search(r"/dp/([A-Z0-9]{10})", link.get("href", ""), re.I)
            if not match:
                continue
            candidate = match.group(1).upper()
            if candidate in seen:
                continue
            title = (
                clean(link.get("title"))
                or node_text(link.select_one("img[alt]"))
                or clean(
                    (link.select_one("img[alt]") or {}).get("alt")
                    if link.select_one("img[alt]")
                    else None
                )
                or node_text(link)
            )
            if not title:
                continue
            if not is_product_recommendation(title):
                promotions_omitted += 1
                continue
            seen.add(candidate)
            rows.append(
                {"asin": candidate, "title": title, "url": f"{BASE_URL}/dp/{candidate}"}
            )
            if len(rows) >= limit:
                break
        return {
            "operation": "amazon.recommendations",
            "asin": product["asin"],
            "source": "product_page_related_links",
            "count": len(rows),
            "promotions_omitted": promotions_omitted,
            "products": rows,
            "meta": response_meta(response),
        }

    def best_sellers(
        self, department: str | None = None, limit: int = 20
    ) -> dict[str, Any]:
        if limit < 1 or limit > 100:
            raise AgentWebError("limit must be between 1 and 100")
        if department and not re.fullmatch(r"[A-Za-z0-9_-]+", department):
            raise AgentWebError(
                "department must be a safe Amazon best-seller category slug"
            )
        suffix = f"/{quote(department)}" if department else ""
        response = self._request(
            "GET",
            f"{BASE_URL}/Best-Sellers/zgbs{suffix}",
            cache_action="best_sellers",
            cache_arguments={"department": department},
            cache_ttl=300,
        )
        soup = soup_for(response)
        rows = []
        seen = set()
        for container in soup.select(
            '[id^="gridItemRoot"], .zg-grid-general-faceout, '
            ".p13n-sc-uncoverable-faceout"
        ):
            link = container.select_one('a[href*="/dp/"]')
            if not link:
                continue
            match = re.search(r"/dp/([A-Z0-9]{10})", link.get("href", ""), re.I)
            if not match or match.group(1).upper() in seen:
                continue
            candidate = match.group(1).upper()
            seen.add(candidate)
            image = container.select_one("img[alt]")
            title_link = next(
                (
                    item
                    for item in container.select('a[href*="/dp/"]')
                    if node_text(item) and parse_money(node_text(item)) is None
                ),
                None,
            )
            price_text = first_text(
                container,
                [
                    ".a-price .a-offscreen",
                    ".p13n-sc-price",
                    '[class*="p13n-sc-price_"]',
                    ".a-color-price",
                ],
            )
            # Amazon hashes the best-seller price class. The complete card text
            # is a safe final fallback and still keeps parsing scoped to one ASIN.
            price = parse_money(price_text) or parse_money(node_text(container))
            rank_text = first_text(container, [".zg-bdg-text"])
            rank_match = re.search(r"#([0-9]+)", rank_text or "")
            rows.append(
                {
                    "rank": int(rank_match.group(1)) if rank_match else len(rows) + 1,
                    "asin": candidate,
                    "title": (
                        clean(image.get("alt")) if image and image.get("alt") else None
                    )
                    or node_text(title_link)
                    or node_text(link),
                    "price": price,
                    "price_text": price_text,
                    "rating": first_text(container, [".a-icon-alt"]),
                    "url": f"{BASE_URL}/dp/{candidate}",
                }
            )
            if len(rows) >= limit:
                break
        if not rows:
            # The top-level /Best-Sellers/zgbs page is a category landing page,
            # not a product grid, so a departmentless call parsed to an empty
            # list that looked like a legitimate result. Fail honestly instead.
            if department is None:
                raise AgentWebError(
                    "Amazon's top-level best-sellers page does not list products "
                    "directly; pass a department slug (e.g. electronics, books, "
                    "toys-and-games) to get a ranked list.",
                    code="department_required",
                    retryable=False,
                )
            raise AgentWebError(
                f"Amazon returned no best-seller products for department "
                f"{department!r}; check that the slug is a valid best-seller "
                f"category.",
                code="empty_result",
                retryable=True,
            )
        return {
            "operation": "amazon.best_sellers",
            "department": department,
            "count": len(rows),
            "products": rows,
            "meta": response_meta(response),
        }

    def sale_check(self, asin: str) -> dict[str, Any]:
        value, _, response = self._product(asin)
        return {
            "operation": "amazon.sale_check",
            "asin": value["asin"],
            "title": value["title"],
            "on_sale": value["on_sale"],
            "price": value["price"],
            "list_price": value["list_price"],
            "discount_percent": value["discount_percent"],
            "deal_badge": value["deal_badge"],
            "meta": response_meta(response),
        }

    def deals(self, query: str | None = None, limit: int = 20) -> dict[str, Any]:
        if limit < 1 or limit > 100:
            raise AgentWebError("limit must be between 1 and 100")
        if query is not None and not query.strip():
            raise AgentWebError("query cannot be empty")
        if query:
            # Amazon's Deals page sends its search box to /s with the
            # search-alias=todays-deals scope. Replaying that upstream query is
            # materially different from substring-filtering arbitrary deal-card
            # prose, which can contain compatibility and promotional terms.
            rows = []
            seen: set[str] = set()
            elapsed_ms = 0.0
            request_count = 0
            organic_match_count = 0
            for page in range(1, (limit - 1) // 50 + 2):
                searched = self.search(
                    query.strip(),
                    limit=min(50, limit - len(rows)),
                    page=page,
                    department="todays-deals",
                )
                request_count += 1
                elapsed_ms += float(searched["meta"]["elapsed_ms"])
                organic_match_count += int(searched["organic_match_count"])
                for item in searched["results"]:
                    if item["asin"] in seen:
                        continue
                    seen.add(item["asin"])
                    rows.append(
                        {
                            "asin": item["asin"],
                            "title": item["title"],
                            "price": item["price"],
                            "price_text": item["price_text"],
                            "list_price": item["list_price"],
                            "discount_percent": item["discount_percent"],
                            "rating": item["rating"],
                            "review_count": item["review_count"],
                            "url": item["url"],
                        }
                    )
                    if len(rows) >= limit:
                        break
                if len(rows) >= limit or not searched["results"]:
                    break
            return {
                "operation": "amazon.deals",
                "query": query,
                "query_applied_by": "amazon_search_alias_todays_deals",
                "organic_match_count": organic_match_count,
                "count": len(rows),
                "deals": rows,
                "meta": {
                    "elapsed_ms": round(elapsed_ms, 1),
                    "request_count": request_count,
                    "transport": searched["meta"].get("transport"),
                },
            }
        response = self._request(
            "GET",
            BASE_URL + "/deals",
            cache_action="deals",
            cache_arguments={},
            cache_ttl=120,
        )
        soup = soup_for(response)
        rows = []
        seen: set[str] = set()
        for link in soup.select('a[href*="/dp/"]'):
            match = re.search(r"/dp/([A-Z0-9]{10})", link.get("href", ""), re.I)
            if not match:
                continue
            asin = match.group(1).upper()
            if asin in seen:
                continue
            text = node_text(link)
            if not text or (query and query.lower() not in text.lower()):
                continue
            seen.add(asin)
            discount_match = re.search(r"([0-9]{1,2})%\s*off", text, re.I)
            prices = re.findall(r"\$[0-9][0-9,]*(?:\.[0-9]{2})?", text)
            rows.append(
                {
                    "asin": asin,
                    "title": re.split(
                        r"\s+[0-9]{1,2}%\s*off", text, maxsplit=1, flags=re.I
                    )[0],
                    "discount_percent": int(discount_match.group(1))
                    if discount_match
                    else None,
                    "prices_shown": prices[:3],
                    "url": urljoin(BASE_URL, link["href"]),
                }
            )
            if len(rows) >= limit:
                break
        return {
            "operation": "amazon.deals",
            "query": query,
            "query_applied_by": None,
            "count": len(rows),
            "deals": rows,
            "meta": response_meta(response),
        }

    def _cart_from_soup(
        self, soup: BeautifulSoup, response: Response
    ) -> dict[str, Any]:
        items = []
        for item in soup.select('.sc-list-item[data-asin][data-itemtype="active"]'):
            asin = clean(item.get("data-asin"))
            if not asin:
                continue
            quantity = item.get("data-quantity")
            raw_data_price = clean(item.get("data-price"))
            unit_price = (
                parse_money(f"${raw_data_price}") if raw_data_price else None
            ) or parse_money(
                first_text(
                    item,
                    [
                        ".sc-product-price",
                        ".sc-item-price-block .a-price .a-offscreen",
                        ".a-price .a-offscreen",
                    ],
                )
            )
            items.append(
                {
                    "asin": asin,
                    "item_id": item.get("data-itemid"),
                    "title": item.get("data-producttitle")
                    or first_text(item, [".sc-product-title"]),
                    "price": unit_price,
                    "quantity": int(quantity)
                    if quantity and quantity.isdigit()
                    else None,
                    "availability": "out_of_stock"
                    if item.get("data-outofstock") == "1"
                    else "available",
                    "url": f"{BASE_URL}/dp/{asin}",
                }
            )
        subtotal_text = first_text(
            soup,
            ["#sc-subtotal-amount-activecart", "#sc-subtotal-label-activecart"],
        )
        subtotal = parse_money(subtotal_text)
        if not items and subtotal is None:
            subtotal = 0.0
            subtotal_text = subtotal_text or "$0.00"
        cart_scope = self._cart_scope()
        anonymous = cart_scope == "anonymous"
        return {
            "operation": "amazon.cart",
            "cart_scope": cart_scope,
            "profile": self.context.profile,
            "visible_in_other_signed_in_browsers": not anonymous,
            "can_open_checkout_in_normal_browser": not anonymous,
            "warning": (
                "This anonymous cart exists only in the AgentWeb profile. It will not appear in your normal browser or Amazon account."
                if anonymous
                else None
            ),
            "count": sum(item.get("quantity") or 0 for item in items),
            "distinct_items": len(items),
            "subtotal": subtotal,
            "subtotal_text": subtotal_text,
            "items": items,
            "checkout_url": BASE_URL + "/checkout/entry/cart",
            "meta": response_meta(response),
        }

    def _get_cart(self) -> tuple[dict[str, Any], BeautifulSoup, Response]:
        response = self._request("GET", BASE_URL + "/gp/cart/view.html")
        soup = soup_for(response)
        return self._cart_from_soup(soup, response), soup, response

    def cart(self) -> dict[str, Any]:
        value, _, _ = self._get_cart()
        return value

    def add_to_cart(
        self, asin: str, quantity: int = 1, cart_scope: str = "account"
    ) -> dict[str, Any]:
        if quantity < 1 or quantity > 999:
            raise AgentWebError("quantity must be between 1 and 999")
        self._validate_cart_scope(cart_scope)
        product, soup, response = self._product(asin, cache=False)
        form = soup.select_one("form#addToCart")
        if not form or not form.select_one("[name='submit.add-to-cart']"):
            raise AgentWebError(
                "This product page does not currently offer Add to Cart"
            )
        fields: list[tuple[str, str]] = []
        for item in form.select("input[name]"):
            name = item.get("name")
            input_type = (item.get("type") or "").lower()
            if not name or input_type in {"button", "submit"}:
                continue
            if input_type in {"checkbox", "radio"} and not item.has_attr("checked"):
                continue
            if name in {
                "quantity",
                "items[0.base][quantity]",
                "submit.buy-now",
                "pipelineType",
                "isBuyNow",
                "isEligibilityLogicDisabled",
            }:
                continue
            fields.append((name, item.get("value", "")))
        fields.extend(
            [
                ("items[0.base][quantity]", str(quantity)),
                ("quantity", str(quantity)),
                ("submit.add-to-cart", "Add to cart"),
            ]
        )
        added = self._request(
            "POST",
            BASE_URL + "/cart/add-to-cart",
            form=fields,
            referer=response.url,
        )
        added_soup = soup_for(added)
        confirmed = bool(added_soup.select_one("#sw-atc-confirmation"))
        cart, _, _ = self._get_cart()
        cart_item = next(
            (item for item in cart["items"] if item["asin"] == product["asin"]),
            None,
        )
        found = cart_item is not None
        if not confirmed and not found:
            raise AgentWebError("Amazon did not confirm that the item entered the cart")
        listed_price = product.get("price")
        cart_price = cart_item.get("price") if cart_item else None
        price_changed = bool(
            listed_price is not None
            and cart_price is not None
            and abs(listed_price - cart_price) >= 0.01
        )
        return {
            "operation": "amazon.add_to_cart",
            "added": True,
            "asin": product["asin"],
            "quantity_requested": quantity,
            "cart_scope": cart_scope,
            "profile": self.context.profile,
            "visible_in_other_signed_in_browsers": cart_scope == "account",
            "warning": cart.get("warning"),
            "listed_unit_price_before_add": listed_price,
            "actual_unit_price_in_cart": cart_price,
            "price_changed_after_add": price_changed,
            "price_warning": (
                f"Amazon showed {listed_price:.2f} before add but the cart contains the item at {cart_price:.2f}. Use the cart price for budget decisions."
                if price_changed
                else None
            ),
            "cart": cart,
            "meta": response_meta(added),
        }

    def remove_from_cart(
        self, asin: str, cart_scope: str = "account"
    ) -> dict[str, Any]:
        self._validate_cart_scope(cart_scope)
        target = parse_asin(asin)
        cart, soup, response = self._get_cart()
        item = soup.select_one(
            f'.sc-list-item[data-asin="{target}"][data-itemtype="active"]'
        )
        if not item:
            raise AgentWebError(f"ASIN {target} is not in this profile's cart")
        item_id = item.get("data-itemid")
        form = soup.select_one("form#activeCartViewForm")
        token = form.select_one('input[name="anti-csrftoken-a2z"]') if form else None
        if not form or not item_id or not token:
            raise AgentWebError(
                "Amazon cart did not expose the expected deletion token"
            )
        action = urljoin(BASE_URL, form.get("action") or "/cart")
        removed = self._request(
            "POST",
            action,
            form=[
                ("anti-csrftoken-a2z", token.get("value", "")),
                (f"submit.delete-active.{item_id}", "Delete"),
            ],
            referer=response.url,
        )
        soup_for(removed)
        updated, _, _ = self._get_cart()
        if any(row["asin"] == target for row in updated["items"]):
            raise AgentWebError("Amazon did not remove the requested item")
        return {
            "operation": "amazon.remove_from_cart",
            "removed": True,
            "asin": target,
            "cart_scope": cart_scope,
            "profile": self.context.profile,
            "warning": updated.get("warning"),
            "cart": updated,
            "meta": response_meta(removed),
        }

    def account_status(self) -> dict[str, Any]:
        response = self._request("GET", BASE_URL + "/gp/css/homepage.html")
        if response.status in {403, 429}:
            has_auth_cookies = self._has_auth_cookies()
            return {
                "operation": "amazon.account_status",
                "signed_in": False,
                "account_label": None,
                "account_href": None,
                "profile": self.context.profile,
                "cookie_count": self.session().cookie_summary()["count"],
                "cookie_candidate": has_auth_cookies,
                "verification": "challenge_required",
                "challenge": {
                    "status": response.status,
                    "retryable": True,
                    "next_action": "agentweb connect amazon --mode session",
                },
                "session": self.session_freshness(
                    False, state="challenge_required"
                ),
                "warning": (
                    "Amazon blocked direct session verification. Saved cookies "
                    "are not proof of a usable login; refresh the protected "
                    "session and do not report connected until verification passes."
                ),
                "meta": response_meta(response),
            }
        try:
            soup = soup_for(response)
        except AgentWebError as exc:
            if exc.code not in {
                "amazon_human_verification_required",
                "amazon_challenge_failed",
                "amazon_challenge_repeated",
            }:
                raise
            return {
                "operation": "amazon.account_status",
                "signed_in": False,
                "account_label": None,
                "account_href": None,
                "profile": self.context.profile,
                "cookie_count": self.session().cookie_summary()["count"],
                "cookie_candidate": self._has_auth_cookies(),
                "verification": "challenge_required",
                "challenge": exc.as_dict(),
                "session": self.session_freshness(
                    False, state="challenge_required"
                ),
                "warning": (
                    "Amazon presented a human or anti-bot challenge, so AgentWeb "
                    "cannot verify this account session yet."
                ),
                "meta": response_meta(response),
            }
        label = first_text(soup, ["#nav-link-accountList-nav-line-1"])
        account_link = soup.select_one("#nav-link-accountList")
        account_href = account_link.get("href") if account_link else None
        signed_in = (
            "/ap/signin" not in response.url
            and bool(label)
            and label.strip().lower() != "hello, sign in"
            and not (account_href and "/ap/signin" in account_href)
            and soup.select_one("form[name=signIn]") is None
        )
        return {
            "operation": "amazon.account_status",
            "signed_in": signed_in,
            "account_label": label,
            "account_href": account_href,
            "profile": self.context.profile,
            "cookie_count": self.session().cookie_summary()["count"],
            "cookie_candidate": self._has_auth_cookies(),
            "verification": "account_navigation" if signed_in else "signed_out",
            "session": self.session_freshness(signed_in),
            "meta": response_meta(response),
        }

    def lists(self) -> dict[str, Any]:
        """Read the signed-in account's Amazon Lists navigation."""
        response = self._request(
            "GET", BASE_URL + "/hz/wishlist/ls", params={"isYourLists": "true"}
        )
        soup = soup_for(response)
        if "/ap/signin" in response.url or soup.select_one("form[name=signIn]"):
            raise AuthenticationRequired(
                "Amazon Lists require a signed-in profile. Run `agentweb connect amazon` once."
            )
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for link in soup.select('a[id^="wl-list-link-"][href*="/hz/wishlist/ls/"]'):
            list_id = str(link.get("id") or "").removeprefix("wl-list-link-")
            if not re.fullmatch(r"[A-Z0-9]{8,30}", list_id) or list_id in seen:
                continue
            seen.add(list_id)
            text = node_text(link) or ""
            privacy = next(
                (
                    value.lower()
                    for value in ("Private", "Shared", "Public")
                    if value in text
                ),
                None,
            )
            name = clean(
                re.sub(
                    r"\s+(?:Default List\s+)?(?:Private|Shared|Public)\s*$", "", text
                )
            )
            suspected_test_artifact = bool(
                re.search(
                    r"\b(?:sitepack|agentweb|qa|test|temporary|scratch)\b",
                    name or "",
                    re.I,
                )
            )
            rows.append(
                {
                    "list_id": list_id,
                    "name": name,
                    "privacy": privacy,
                    "default": "Default List" in text,
                    "suspected_test_artifact": suspected_test_artifact,
                    "safe_for_implicit_writes": False,
                    "url": BASE_URL + f"/hz/wishlist/ls/{list_id}",
                }
            )
        warnings = [
            "Amazon's default flag is account state, not permission to choose a write destination. Any future list mutation must receive an explicit list_id."
        ]
        if any(row["default"] and row["suspected_test_artifact"] for row in rows):
            warnings.append(
                "The account default list appears to be QA or temporary data. Do not add items to it implicitly."
            )
        return {
            "operation": "amazon.lists",
            "count": len(rows),
            "lists": rows,
            "write_destination_policy": "explicit_list_id_required",
            "warnings": warnings,
            "profile": self.context.profile,
            "meta": response_meta(response),
        }

    def list(self, list_id: str) -> dict[str, Any]:
        """Read one Amazon List and the item cards currently rendered on it."""
        if not re.fullmatch(r"[A-Za-z0-9]{8,30}", list_id):
            raise AgentWebError("list_id must be an 8-30 character Amazon List ID")
        list_id = list_id.upper()
        response = self._request("GET", BASE_URL + f"/hz/wishlist/ls/{list_id}")
        soup = soup_for(response)
        if "/ap/signin" in response.url or soup.select_one("form[name=signIn]"):
            raise AuthenticationRequired(
                "This Amazon List requires a signed-in profile. Run `agentweb connect amazon` once."
            )
        name = first_text(
            soup,
            ["#profile-list-name", "#list-name", "h1.a-size-large", "h1"],
        )
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for card in soup.select('[data-itemid], li[id^="item_"]'):
            item_id = clean(card.get("data-itemid"))
            asin = clean(card.get("data-reposition-action-params"))
            asin_match = ASIN_PATTERN.search(asin or "")
            if not asin_match:
                product_link = card.select_one('a[href*="/dp/"]')
                asin_match = (
                    ASIN_PATTERN.search(product_link.get("href", ""))
                    if product_link
                    else None
                )
            asin_value = asin_match.group(1).upper() if asin_match else None
            identity = item_id or asin_value
            if not identity or identity in seen:
                continue
            seen.add(identity)
            title = first_text(
                card,
                [
                    "#itemName_" + item_id if item_id else "[data-item-title]",
                    ".a-link-normal[href*='/dp/']",
                    "h2",
                    "h3",
                ],
            )
            items.append(
                {
                    "item_id": item_id,
                    "asin": asin_value,
                    "title": title,
                    "price": parse_money(
                        first_text(card, [".a-price .a-offscreen", ".a-price"])
                    ),
                    "url": BASE_URL + f"/dp/{asin_value}" if asin_value else None,
                }
            )
        if not name and not items:
            raise AgentWebError(
                f"Amazon List {list_id} was not found or is not visible to this profile",
                code="amazon_list_not_found",
            )
        return {
            "operation": "amazon.list",
            "list_id": list_id,
            "name": name,
            "count": len(items),
            "items": items,
            "url": response.url,
            "meta": response_meta(response),
        }

    def _wishlist_page(
        self, list_id: str
    ) -> tuple[BeautifulSoup, Response]:
        """Load an Amazon List page for a signed-in profile, or fail loudly."""
        response = self._request("GET", BASE_URL + f"/hz/wishlist/ls/{list_id}")
        soup = soup_for(response)
        if "/ap/signin" in response.url or soup.select_one("form[name=signIn]"):
            raise AuthenticationRequired(
                "Amazon Lists require a signed-in profile. Run `agentweb connect amazon` once."
            )
        return soup, response

    @staticmethod
    def _wishlist_anti_csrf(soup: BeautifulSoup) -> str | None:
        node = soup.select_one('input[name="anti-csrftoken-a2z"]')
        if node and node.get("value"):
            return str(node.get("value"))
        meta = soup.select_one('meta[name="anti-csrftoken-a2z"]')
        if meta and meta.get("content"):
            return str(meta.get("content"))
        return None

    def add_to_list(
        self, asin: str, list_id: str, quantity: int = 1, confirm: bool = False
    ) -> dict[str, Any]:
        """Add a product to a specific Amazon List (wishlist).

        UNVERIFIED: rides Amazon's reverse-engineered wishlist add-items
        endpoint. The result is confirmed by reading the list back, so a
        success here means the item is actually present on the list.
        """
        self.require_confirm(confirm, "amazon.add_to_list")
        target = parse_asin(asin)
        if not re.fullmatch(r"[A-Za-z0-9]{8,30}", list_id):
            raise AgentWebError(
                "list_id must be an 8-30 character Amazon List ID",
                code="invalid_input",
            )
        list_id = list_id.upper()
        if quantity < 1 or quantity > 999:
            raise AgentWebError(
                "quantity must be between 1 and 999", code="invalid_input"
            )
        if not self._has_auth_cookies():
            raise AuthenticationRequired(
                "Adding to an Amazon List requires signing in once. Run `agentweb connect amazon`."
            )
        soup, page = self._wishlist_page(list_id)
        token = self._wishlist_anti_csrf(soup)
        if not token:
            raise AgentWebError(
                "Amazon List page did not expose the expected write token",
                code="website_replay_changed",
                retryable=True,
            )
        self._request(
            "POST",
            BASE_URL + "/hz/wishlist/add-items",
            json_body={
                "listId": list_id,
                "items": [{"asin": target, "quantity": quantity}],
            },
            referer=page.url,
            headers={
                "anti-csrftoken-a2z": token,
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
            },
        )
        updated = self.list(list_id)
        present = any(row.get("asin") == target for row in updated["items"])
        if not present:
            raise AgentWebError(
                "Amazon did not confirm the item was added to the list",
                code="amazon_list_add_not_confirmed",
                retryable=True,
                next_action="verify the ASIN is purchasable and the list_id is writable, then retry",
            )
        return {
            "operation": "amazon.add_to_list",
            "added": True,
            "asin": target,
            "list_id": list_id,
            "quantity_requested": quantity,
            "profile": self.context.profile,
            "list": updated,
            "meta": updated.get("meta"),
        }

    def remove_from_list(
        self, list_id: str, item_id: str, confirm: bool = False
    ) -> dict[str, Any]:
        """Remove an item from a specific Amazon List (wishlist).

        UNVERIFIED: rides Amazon's reverse-engineered wishlist delete endpoint.
        The removal is confirmed by reading the list back.
        """
        self.require_confirm(confirm, "amazon.remove_from_list")
        if not re.fullmatch(r"[A-Za-z0-9]{8,30}", list_id):
            raise AgentWebError(
                "list_id must be an 8-30 character Amazon List ID",
                code="invalid_input",
            )
        if not re.fullmatch(r"[A-Za-z0-9._-]{5,80}", item_id):
            raise AgentWebError(
                "item_id must be an Amazon List item id (see amazon.list)",
                code="invalid_input",
            )
        list_id = list_id.upper()
        if not self._has_auth_cookies():
            raise AuthenticationRequired(
                "Editing an Amazon List requires signing in once. Run `agentweb connect amazon`."
            )
        soup, page = self._wishlist_page(list_id)
        if not soup.select_one(f'[data-itemid="{item_id}"]'):
            raise AgentWebError(
                f"Item {item_id} is not on Amazon List {list_id}",
                code="amazon_list_item_not_found",
            )
        token = self._wishlist_anti_csrf(soup)
        if not token:
            raise AgentWebError(
                "Amazon List page did not expose the expected write token",
                code="website_replay_changed",
                retryable=True,
            )
        self._request(
            "POST",
            BASE_URL + "/hz/wishlist/item/delete",
            json_body={"listId": list_id, "itemId": item_id},
            referer=page.url,
            headers={
                "anti-csrftoken-a2z": token,
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
            },
        )
        updated = self.list(list_id)
        still_present = any(
            row.get("item_id") == item_id for row in updated["items"]
        )
        if still_present:
            raise AgentWebError(
                "Amazon did not confirm the item was removed from the list",
                code="amazon_list_remove_not_confirmed",
                retryable=True,
            )
        return {
            "operation": "amazon.remove_from_list",
            "removed": True,
            "list_id": list_id,
            "item_id": item_id,
            "profile": self.context.profile,
            "list": updated,
            "meta": updated.get("meta"),
        }

    def orders(self, limit: int = 10) -> dict[str, Any]:
        if limit < 1 or limit > 100:
            raise AgentWebError("limit must be between 1 and 100")
        response = self._request("GET", BASE_URL + "/gp/your-account/order-history")
        soup = soup_for(response)
        if "/ap/signin" in response.url or soup.select_one("form[name=signIn]"):
            # Amazon can require a fresh password/OTP challenge for Your Orders
            # while the same retained session remains signed in elsewhere.
            account = self.account_status()
            if account.get("signed_in"):
                raise AgentWebError(
                    "Amazon accepted the account session but requires an additional password, OTP, or security step for Your Orders.",
                    code="additional_authentication_required",
                    next_action="site_connect(site=amazon, mode=session)",
                    user_action=(
                        "Refresh the Amazon session once and complete Amazon's security "
                        "checkpoint; logging in again is not the same requirement."
                    ),
                    details={
                        "account_signed_in": True,
                        "protected_area": "orders",
                    },
                )
            raise AuthenticationRequired(
                "Amazon orders require a signed-in profile. Run `agentweb connect amazon` once."
            )
        rows = []
        cards = soup.select(".order-card, .order, [data-order-id]")
        for card in cards:
            text = node_text(card) or ""
            order_id = card.get("data-order-id")
            if not order_id:
                match = re.search(r"Order\s*#?\s*([0-9-]{10,})", text, re.I)
                order_id = match.group(1) if match else None
            title_nodes = card.select(
                ".yohtmlc-product-title, .a-link-normal[href*='/dp/']"
            )
            rows.append(
                {
                    "order_id": order_id,
                    "summary": text[:1200],
                    "items": [
                        node_text(node) for node in title_nodes if node_text(node)
                    ][:20],
                }
            )
            if len(rows) >= limit:
                break
        return {
            "operation": "amazon.orders",
            "count": len(rows),
            "orders": rows,
            "meta": response_meta(response),
        }

    def checkout_url(self) -> dict[str, Any]:
        return {
            "operation": "amazon.checkout_url",
            "url": BASE_URL + "/checkout/entry/cart",
            "opened": False,
            "order_placed": False,
            "note": "Use amazon.checkout to inspect the live checkout or explicitly place the cart order.",
        }

    def payment_methods(self) -> dict[str, Any]:
        """List checkout-visible saved payment instruments without exposing tokens."""
        self._validate_cart_scope("account")
        response, navigation = self._checkout_document()
        step = self._checkout_step(response)
        if step["name"] == "delivery_address_required":
            raise AgentWebError(
                "Amazon needs a delivery address before it will expose payment methods.",
                code="amazon_delivery_address_required",
                next_action="amazon.add_address",
            )
        soup = soup_for(response)
        methods = []
        for index, item in enumerate(
            soup.select("input[name='ppw-instrumentRowSelection']"), start=1
        ):
            container = item.find_parent("div", class_="pmts-instrument-box")
            label_node = item.find_parent("label")
            label = (
                node_text(label_node)
                or node_text(container)
                or f"Payment method {index}"
            )
            container_text = (node_text(container) or label or "").lower()
            value = str(item.get("value") or "")
            kind_match = re.search(r"(?:^|&)paymentMethod=([^&]+)", value)
            available = (
                not item.has_attr("disabled") and "ineligible" not in container_text
            )
            methods.append(
                {
                    "index": index,
                    "label": label,
                    "kind": kind_match.group(1) if kind_match else None,
                    "selected": item.has_attr("checked"),
                    "available": available,
                    "unavailable_reason": (
                        node_text(container) if not available else None
                    ),
                }
            )
        if not methods:
            selected = self._selected_payment_method(soup)
            if selected:
                methods.append(selected)
        available_count = sum(1 for item in methods if item["available"])
        return {
            "operation": "amazon.payment_methods",
            "checkout_step": step["name"],
            "count": len(methods),
            "available_count": available_count,
            "payment_methods": methods,
            "requires_new_payment_method": available_count == 0,
            "checkout_navigation": navigation,
            "agent_instruction": (
                "No usable saved payment method exists. Ask the user how they want to pay; "
                "do not ask for the delivery address again."
                if available_count == 0
                else "Use a checkout-visible saved payment method; never request raw card details when a saved method is available."
            ),
            "meta": response_meta(response),
        }

    @staticmethod
    def _checkout_total(soup: BeautifulSoup) -> tuple[str | None, float | None]:
        total_text = first_text(
            soup,
            [
                "#subtotals-marketplace-table .grand-total-price",
                "#order-summary .grand-total-price",
                "[data-testid='order-total']",
                ".grand-total-price",
            ],
        )
        if not total_text:
            match = re.search(
                r"(?:Order total|Grand Total)\s*:?\s*((?:US)?\$\s*[0-9][0-9,]*(?:\.[0-9]{1,2})?)",
                soup.get_text(" ", strip=True),
                re.I,
            )
            total_text = clean(match.group(1)) if match else None
        return total_text, parse_money(total_text)

    @staticmethod
    def _place_order_form(soup: BeautifulSoup):
        button = soup.select_one(
            "#placeYourOrder input[type='submit'], #placeYourOrder button, "
            "input[name='placeYourOrder1'], button[name='placeYourOrder1'], "
            "input[name='submitOrderButtonId'], button[name='submitOrderButtonId']"
        )
        if not button:
            return None, None
        return button.find_parent("form"), button

    @staticmethod
    def _checkout_purchase_root(response: Response) -> str | None:
        """Resolve Amazon's per-checkout path from the entry document.

        The checkout entry response is a JavaScript shell.  It embeds the real
        purchase path even though curl never performs the browser-side
        navigation to it.
        """
        match = re.search(
            r"(/checkout/p/p-[0-9]+-[0-9]+-[0-9]+)/(?:address|[^\"'<>\\ ]+)",
            response.text,
            re.I,
        )
        return urljoin(response.url, match.group(1)) if match else None

    def _checkout_document(self) -> tuple[Response, list[str]]:
        """Resolve checkout's server-rendered document without a browser.

        Amazon's entry URL currently returns a JavaScript shell.  The shell
        embeds a per-purchase path whose ``pip`` page may contain an optional
        Prime trial interstitial.  A normal browser reaches the actual order
        review page by choosing "No thanks".  Replay that exact first-party
        navigation and return the resulting checkout document.
        """
        entry = self._request("GET", BASE_URL + "/checkout/entry/cart")
        if "/ap/signin" in entry.url or soup_for(entry).select_one("form[name=signIn]"):
            return entry, []
        purchase_root = self._checkout_purchase_root(entry)
        if not purchase_root:
            return entry, []
        page = self._request("GET", purchase_root + "/pip", referer=entry.url)
        navigation = ["opened_checkout_purchase_page"]
        soup = soup_for(page)
        decline = soup.select_one("#prime-decline-button[href]")
        if decline is None:
            return page, navigation

        decline_url = urljoin(page.url, str(decline.get("href") or ""))
        parsed = urlparse(decline_url)
        current_root = self._checkout_purchase_root(page)
        expected_path = (
            urlparse(current_root).path + "/prime/handler" if current_root else None
        )
        query = parse_qs(parsed.query)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "www.amazon.com"
            or expected_path is None
            or parsed.path != expected_path
            or query.get("action") != ["decline"]
        ):
            raise AgentWebError(
                "Amazon exposed an unexpected Prime-offer checkout transition",
                code="website_replay_changed",
                retryable=True,
            )
        page = self._request("GET", decline_url, referer=page.url)
        navigation.append("declined_optional_prime_offer")
        return page, navigation

    @staticmethod
    def _selected_payment_method(soup: BeautifulSoup) -> dict[str, Any] | None:
        node = soup.select_one(
            "#selected-payment-methods-list-container "
            "#payment-option-text-default, "
            "#selected-payment-methods-list-container [id^='payment-option-text']"
        )
        label = node_text(node)
        if not label:
            return None
        label = re.sub(r"^Paying with\s+", "", label, flags=re.I)
        tail = re.search(r"(?:ending in\s*)?(\d{4})\s*$", label, re.I)
        kind = clean(label[: tail.start()]) if tail else label
        kind = re.sub(r"\s+ending in\s*$", "", kind, flags=re.I) if kind else None
        display = f"{kind} ending in {tail.group(1)}" if tail and kind else label
        return {
            "index": 1,
            "label": display,
            "kind": kind,
            "selected": True,
            "available": True,
            "unavailable_reason": None,
        }

    @classmethod
    def _checkout_step(cls, response: Response) -> dict[str, Any]:
        """Identify the active checkout step from forms, never generic link text."""
        soup = soup_for(response)
        form, button = cls._place_order_form(soup)
        if form is not None and button is not None:
            return {"name": "review", "ready_to_place": True}

        payment_form = soup.select_one("form[action*='/pay/continue']")
        if payment_form is not None:
            instruments = payment_form.select(
                "input[name='ppw-instrumentRowSelection']"
            )
            selected = [item for item in instruments if item.has_attr("checked")]
            return {
                "name": "payment_method_selected"
                if selected
                else "payment_method_required",
                "ready_to_place": False,
                "saved_payment_method_count": len(instruments),
                "selected_payment_method_count": len(selected),
            }

        if soup.select_one(
            "form[name='checkout-auiws-pagelet-form'], "
            "input[name='address-ui-widgets-enterAddressLine1']"
        ):
            return {"name": "delivery_address_required", "ready_to_place": False}
        if re.search(r"/add-new-shipping-address(?:[?&]|&quot;|\")", response.text):
            return {"name": "delivery_address_required", "ready_to_place": False}
        return {"name": "unknown", "ready_to_place": False}

    @staticmethod
    def _form_fields(form) -> list[tuple[str, str]]:
        fields: list[tuple[str, str]] = []
        for item in form.select("input[name], select[name], textarea[name]"):
            name = str(item.get("name") or "")
            if not name:
                continue
            input_type = str(item.get("type") or "").lower()
            if input_type in {"button", "submit", "image", "file"}:
                continue
            if input_type in {"checkbox", "radio"} and not item.has_attr("checked"):
                continue
            if item.name == "select":
                selected = item.select_one("option[selected]") or item.select_one(
                    "option"
                )
                value = str(selected.get("value") or "") if selected else ""
            else:
                value = str(item.get("value") or item.get_text() or "")
            fields.append((name, value))
        return fields

    def add_address(
        self,
        full_name: str,
        phone_number: str,
        address_line1: str,
        city: str,
        state_or_region: str,
        postal_code: str,
        address_line2: str | None = None,
        country_code: str = "US",
        make_default: bool = False,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Add and select a delivery address through Amazon's checkout form."""
        self.require_confirm(confirm, "amazon.add_address")
        required = {
            "full_name": full_name,
            "phone_number": phone_number,
            "address_line1": address_line1,
            "city": city,
            "state_or_region": state_or_region,
            "postal_code": postal_code,
            "country_code": country_code,
        }
        missing = [name for name, value in required.items() if not str(value).strip()]
        if missing:
            raise AgentWebError(
                f"Missing required address fields: {', '.join(missing)}",
                code="invalid_input",
                details={"missing_fields": missing},
            )
        if len(country_code.strip()) != 2:
            raise AgentWebError("country_code must be a two-letter code")

        entry = self._request("GET", BASE_URL + "/checkout/entry/cart")
        if "/ap/signin" in entry.url or soup_for(entry).select_one("form[name=signIn]"):
            raise AuthenticationRequired(
                "Amazon checkout requires a signed-in profile. Run `agentweb connect amazon` once."
            )
        purchase_root = self._checkout_purchase_root(entry)
        if not purchase_root:
            raise AgentWebError(
                "Amazon did not expose the current checkout identifier",
                code="website_replay_changed",
                retryable=True,
            )
        form_page = self._request(
            "GET",
            purchase_root + "/add-new-shipping-address",
            params={"referrer": "address", "isInline": "1", "isAsync": "1"},
            referer=entry.url,
            headers={"X-Requested-With": "XMLHttpRequest", "Accept": "text/html,*/*"},
        )
        form_soup = soup_for(form_page)
        form = form_soup.select_one(
            "form#main-continue-form, form[name='checkout-auiws-pagelet-form']"
        )
        if form is None:
            raise AgentWebError(
                "Amazon did not expose its checkout address form",
                code="website_replay_changed",
                retryable=True,
            )

        overrides = {
            "address-ui-widgets-countryCode": country_code.strip().upper(),
            "address-ui-widgets-enterAddressFullName": full_name.strip(),
            "address-ui-widgets-enterAddressPhoneNumber": phone_number.strip(),
            "address-ui-widgets-enterAddressLine1": address_line1.strip(),
            "address-ui-widgets-enterAddressLine2": (address_line2 or "").strip(),
            "address-ui-widgets-enterAddressCity": city.strip(),
            "address-ui-widgets-enterAddressStateOrRegion": state_or_region.strip(),
            "address-ui-widgets-enterAddressPostalCode": postal_code.strip(),
        }
        fields = self._form_fields(form)
        fields = [(name, value) for name, value in fields if name not in overrides]
        fields.extend(overrides.items())
        fields = [
            (name, value)
            for name, value in fields
            if name != "address-ui-widgets-use-as-my-default"
        ]
        if make_default:
            fields.append(("address-ui-widgets-use-as-my-default", "true"))
        action = urljoin(form_page.url, str(form.get("action") or ""))
        submitted = self._request(
            "POST",
            action,
            form=fields,
            referer=form_page.url,
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "text/plain, */*; q=0.01",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8;",
            },
        )
        result_soup = soup_for(submitted)
        errors = []
        for node in result_soup.select(
            ".a-alert-error .a-alert-content, [id$='-error'] .a-alert-content, "
            "[role='alert']"
        ):
            message = node_text(node)
            if message and message not in errors:
                errors.append(message)
        still_editing = bool(
            result_soup.select_one(
                "input[name='address-ui-widgets-enterAddressLine1'], "
                "form[name='checkout-auiws-pagelet-form']"
            )
        )
        if errors:
            raise AgentWebError(
                "Amazon did not accept the delivery address without another choice or correction.",
                code="amazon_address_confirmation_required",
                retryable=True,
                next_action="correct the reported fields or choose Amazon's suggested address, then retry amazon.add_address",
                details={"validation_messages": errors[:10]},
            )

        # Amazon commits this XHR asynchronously.  A disappeared response form
        # is not proof of success; poll a fresh entry document until its active
        # form advances away from the address step.
        verified_response: Response | None = None
        verified_step: dict[str, Any] | None = None
        for attempt in range(6):
            if attempt:
                time.sleep(0.5)
            candidate = self._request("GET", BASE_URL + "/checkout/entry/cart")
            step = self._checkout_step(candidate)
            if step["name"] != "delivery_address_required":
                verified_response = candidate
                verified_step = step
                break
        if verified_response is None or verified_step is None:
            raise AgentWebError(
                "Amazon received the address form but a fresh checkout still requires a delivery address.",
                code="amazon_address_not_persisted",
                retryable=True,
                next_action="retry amazon.add_address once with the corrected address; do not claim success unless checkout advances",
                details={
                    "validation_messages": [],
                    "form_response_still_editing": still_editing,
                },
            )
        return {
            "operation": "amazon.add_address",
            "address_added": True,
            "selected_for_checkout": True,
            "make_default": make_default,
            "city": city.strip(),
            "state_or_region": state_or_region.strip(),
            "postal_code": postal_code.strip(),
            "country_code": country_code.strip().upper(),
            "verified": True,
            "verification": "fresh_checkout_advanced",
            "checkout_step_after": verified_step["name"],
            "next_operation": "amazon.checkout",
            "meta": response_meta(verified_response),
        }

    @staticmethod
    def _successful_order(
        soup: BeautifulSoup, response: Response
    ) -> tuple[bool, str | None]:
        text = soup.get_text(" ", strip=True)
        match = re.search(r"Order\s*(?:number|#)\s*:?\s*([0-9-]{10,})", text, re.I)
        order_id = match.group(1) if match else None
        confirmed = bool(
            order_id
            or soup.select_one(
                "#thank-you, .thank-you, [data-testid='order-confirmation']"
            )
            or "thank you, your order has been placed" in text.lower()
            or "/thankyou" in response.url.lower()
        )
        return confirmed, order_id

    def checkout(
        self,
        place_order: bool = False,
        expected_total: float | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Inspect checkout or place the cart order after explicit confirmation.

        Amazon keeps payment credentials tokenized in the retained account
        session. AgentWeb submits only the checkout document's existing opaque
        fields; it never asks an agent to transmit a raw card number or CVV.
        """
        self._validate_cart_scope("account")
        cart, _, _ = self._get_cart()
        if not cart.get("items"):
            raise AgentWebError("The Amazon account cart is empty")
        response, navigation = self._checkout_document()
        soup = soup_for(response)
        if "/ap/signin" in response.url or soup.select_one("form[name=signIn]"):
            account = self.account_status()
            if account.get("signed_in"):
                raise AgentWebError(
                    "Amazon accepted the normal account session but requires a fresh password, OTP, or security step specifically for checkout.",
                    code="additional_authentication_required",
                    next_action="site_connect(site=amazon, mode=session)",
                    user_action=(
                        "Run `agentweb connect amazon --mode session` once. It opens "
                        "Amazon directly at checkout, waits for the protected checkout "
                        "page rather than the normal account page, and refreshes the "
                        "retained session before the agent retries checkout."
                    ),
                    details={
                        "account_signed_in": True,
                        "protected_area": "checkout",
                    },
                )
            raise AuthenticationRequired(
                "Amazon checkout requires a signed-in profile. Run `agentweb connect amazon` once."
            )
        total_text, total = self._checkout_total(soup)
        form, button = self._place_order_form(soup)
        step = self._checkout_step(response)
        summary = {
            "operation": "amazon.checkout",
            "cart": cart,
            "order_total": total,
            "order_total_text": total_text,
            "ready_to_place": bool(step["ready_to_place"]),
            "checkout_step": step["name"],
            "order_placed": False,
            "verified": False,
            "requires_human_checkpoint": False,
            "checkpoint_reason": None,
            "checkout_navigation": navigation,
            "meta": response_meta(response),
        }
        if form is None:
            if step["name"] == "delivery_address_required":
                summary.update(
                    {
                        "required_inputs": [
                            "full_name",
                            "phone_number",
                            "address_line1",
                            "city",
                            "state_or_region",
                            "postal_code",
                        ],
                        "next_operation": "amazon.add_address",
                        "agent_instruction": (
                            "Ask only for missing address fields, call amazon.add_address with confirm=true, "
                            "then retry amazon.checkout. This is an ordinary mapped website action, not a human-only checkpoint."
                        ),
                    }
                )
            elif step["name"] == "payment_method_required":
                summary.update(
                    {
                        "required_inputs": ["payment_method"],
                        "saved_payment_method_count": step[
                            "saved_payment_method_count"
                        ],
                        "selected_payment_method_count": step[
                            "selected_payment_method_count"
                        ],
                        "next_operation": "amazon.payment_methods",
                        "agent_instruction": (
                            "The delivery address is selected. Inspect saved payment methods next; "
                            "do not ask for the address again and do not claim checkout regressed."
                        ),
                    }
                )
            else:
                summary.update(
                    {
                        "checkout_state_known": False,
                        "checkpoint_reason": None,
                        "next_operation": "amazon.checkout",
                        "agent_instruction": (
                            "AgentWeb could not classify Amazon's checkout document. Do not claim that a "
                            "payment, consent, or security checkpoint exists without evidence. Retry once; "
                            "if it remains unknown, report website_replay_changed."
                        ),
                    }
                )
        if not place_order:
            return summary
        self.require_confirm(confirm, "amazon.checkout")
        if expected_total is not None:
            if expected_total < 0:
                raise AgentWebError("expected_total cannot be negative")
            if total is None:
                raise AgentWebError(
                    "Amazon did not expose an order total, so AgentWeb refused to submit an amount-guarded order"
                )
            if abs(total - expected_total) >= 0.01:
                raise AgentWebError(
                    f"Amazon order total changed: expected {expected_total:.2f}, observed {total:.2f}"
                )
        if form is None or button is None:
            raise AgentWebError(
                "Amazon has not reached its Place your order step. Complete the reported account checkpoint, then retry the same confirmed checkout.",
                code="amazon_checkout_checkpoint_required",
                retryable=True,
                next_action="complete only the Amazon-requested checkpoint, then retry amazon.checkout",
            )
        fields = self._form_fields(form)
        button_name = str(button.get("name") or "")
        if button_name:
            fields.append((button_name, str(button.get("value") or "Place your order")))
        action = urljoin(response.url, str(form.get("action") or response.url))
        method = str(form.get("method") or "POST").upper()
        if method != "POST":
            raise AgentWebError(
                "Amazon exposed an unexpected non-POST order form",
                code="website_replay_changed",
                retryable=False,
            )
        submitted = self._request(
            "POST",
            action,
            form=fields,
            referer=response.url,
        )
        confirmation = soup_for(submitted)
        verified, order_id = self._successful_order(confirmation, submitted)
        if not verified:
            raise AgentWebError(
                "Amazon accepted the checkout submission but AgentWeb could not verify an order confirmation. Check amazon.orders before retrying to avoid a duplicate order.",
                code="amazon_order_confirmation_uncertain",
                retryable=False,
                details={"submission_url": submitted.url},
            )
        return {
            **summary,
            "order_placed": True,
            "verified": True,
            "order_id": order_id,
            "requires_human_checkpoint": False,
            "checkpoint_reason": None,
            "meta": response_meta(submitted),
        }
