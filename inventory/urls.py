from django.urls import path
from . import views
from . import converty

urlpatterns = [
    path("statistiques/commandes/", views.stats_commandes, name="stats_commandes"),
    path("converty/connect/", converty.converty_connect, name="converty_connect"),
    path("converty/resubscribe/", converty.converty_resubscribe, name="converty_resubscribe"),
    path("converty/callback/", converty.converty_callback, name="converty_callback"),
    path("webhooks/converty/", converty.api_converty_webhook, name="api_converty_webhook"),
    path("", views.home_dispatcher, name="home"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("scan/shipping/", views.shipping_scan, name="shipping_scan"),
    path("scan/return/", views.return_scan, name="return_scan"),
    # Internal sale (employee / friend) — admin only
    path("scan/internal-sale/", views.internal_sale_view, name="internal_sale"),
    path("api/internal-sale/lookup/", views.api_internal_sale_lookup, name="api_internal_sale_lookup"),
    path("api/internal-sale/confirm/", views.api_internal_sale_confirm, name="api_internal_sale_confirm"),
    path("stock-value/", views.stock_value, name="stock_value"),
    path("api/scan/shipping/", views.api_scan_shipping, name="api_scan_shipping"),
    path("api/orders/<int:pk>/state/", views.api_get_order_state, name="api_get_order_state"),
    path("api/scan/reception/", views.api_scan_reception, name="api_scan_reception"),
    path("api/scan/return/", views.api_scan_return, name="api_scan_return"),
    path("api/scan/return/multiple/", views.api_return_multiple, name="api_return_multiple"),
    path("api/scan/return/exchange-received/", views.api_exchange_mark_received, name="api_exchange_mark_received"),
    path("api/scan/payment/", views.api_scan_payment, name="api_scan_payment"),
    path("api/scan/payment/confirm/", views.api_confirm_payment, name="api_confirm_payment"),
    path("api/order/remove-unit/", views.api_remove_from_order, name="api_remove_from_order"),
    path("api/order/close/", views.api_close_order, name="api_close_order"),
    path("orders/<int:pk>/", views.order_detail, name="order_detail"),
    path("api/orders/<int:pk>/update-amount/", views.api_update_order_amount, name="api_update_order_amount"),
    path("api/orders/<int:pk>/delete/", views.api_delete_order, name="api_delete_order"),
    path("api/orders/<int:pk>/remove-unit/", views.api_order_remove_unit, name="api_order_remove_unit"),
    path("api/orders/<int:pk>/add-unit/", views.api_order_add_unit, name="api_order_add_unit"),
    path("api/orders/<int:pk>/fix-units/", views.api_fix_order_units, name="api_fix_order_units"),
    path("api/orders/<int:pk>/navex-status/", views.api_navex_status, name="api_navex_status"),
    path("navex-sync/", views.navex_sync, name="navex_sync"),
    path("a-verifier/", views.a_verifier, name="a_verifier"),
    path("api/orders/<int:pk>/mark-treated/", views.api_mark_treated, name="api_mark_treated"),
    path("api/create-return-order/", views.api_create_return_order, name="api_create_return_order"),
    path("api/orders/<int:pk>/return-unit/", views.api_return_unit_to_order, name="api_return_unit_to_order"),
    path("api/variants/<int:variant_pk>/size-alert/<str:size>/", views.api_set_size_alert, name="api_set_size_alert"),
    path("api/variants/<int:variant_pk>/size-alert/<str:size>/get/", views.api_get_size_alert, name="api_get_size_alert"),
    path("api/navex-sync/", views.api_navex_sync, name="api_navex_sync"),
    path("api/scan-session/log/", views.api_log_scan_session, name="api_log_scan_session"),
    path("api/scan-session/today/", views.api_get_scan_session, name="api_get_scan_session"),
    path("api/scan-session/clear-today/", views.api_clear_scan_session_today, name="api_clear_scan_session_today"),
    path("api/scan-session/recheck/", views.api_recheck_session, name="api_recheck_session"),
    path("api/send-email/<str:email_type>/", views.api_send_email, name="api_send_email"),
    path("cron/morning/", views.cron_morning_email, name="cron_morning"),
    path("cron/navex-sync/", views.cron_navex_sync, name="cron_navex_sync"),
    path("test/low-stock-whatsapp/", views.test_low_stock_whatsapp, name="test_low_stock_whatsapp"),
    path("cron/evening/", views.cron_evening_email, name="cron_evening"),
    path("api/orders/<int:pk>/save-navex/", views.api_save_navex_info, name="api_save_navex_info"),
    path("api/orders/<int:pk>/amount/", views.api_get_order_amount, name="api_get_order_amount"),
    path("api/check-duplicate-client/", views.api_check_duplicate_client, name="api_check_duplicate_client"),
    path("api/navex-attente/", views.api_navex_en_attente, name="api_navex_attente"),
    path("api/orders/<int:pk>/confirm-navex/", views.api_confirm_payment_from_navex, name="api_confirm_payment_from_navex"),
    path("products/", views.products_list, name="products_list"),
    path("admin-panel/", views.admin_panel, name="admin_panel"),
    path("revenue/", views.revenue, name="revenue"),
    path("ads-spending/", views.ads_dashboard, name="ads_dashboard"),
    path("products/<int:pk>/", views.product_detail, name="product_detail"),
    path("api/products/<int:pk>/toggle-flag/", views.api_toggle_product_flag, name="api_toggle_product_flag"),
    path("api/products/<int:pk>/check-gaps/", views.api_check_barcode_gaps, name="api_check_barcode_gaps"),
    path("search/", views.search, name="search"),
    path("api/search/", views.api_search, name="api_search"),

    # V1 — Phase 8: unit history (clickable barcode)
    path("unit/<str:barcode>/", views.unit_detail, name="unit_detail"),

    # V2 — Order management (Phase 4) — renamed to /sales-orders/ to avoid
    # colliding with v1's /orders/<pk>/ which routes to ShippingOrder detail.
    path("sales-orders/", views.orders_list, name="orders_list"),
    path("sales-orders/add/", views.order_create, name="order_create"),
    path("sales-orders/<int:pk>/", views.order_view, name="order_view"),
    path("api/sales-orders/<int:pk>/status/", views.api_order_change_status, name="api_order_change_status"),
    path("api/sales-orders/<int:pk>/note/", views.api_order_set_note, name="api_order_set_note"),
    path("api/sales-orders/<int:pk>/push-navex/", views.api_push_order_to_navex, name="api_push_order_to_navex"),
    path("api/sales-orders/sync-navex/", views.api_sync_v2_orders_navex, name="api_sync_v2_orders_navex"),

    # V2 — Inline create + offer APIs
    path("api/sales-orders/create/", views.api_create_order_inline, name="api_create_order_inline"),
    # Phase A — auto-save / draft flow
    path("api/sales-orders/draft/upsert/", views.api_order_draft_upsert, name="api_order_draft_upsert"),
    path("api/sales-orders/<int:pk>/discard/", views.api_order_draft_discard, name="api_order_draft_discard"),
    path("api/sales-orders/<int:pk>/draft/", views.api_order_draft_get, name="api_order_draft_get"),
    # User theme preference (dark/light)
    path("api/user/theme/", views.api_user_theme, name="api_user_theme"),
    # Search
    path("api/sales-orders/search/", views.api_orders_search, name="api_orders_search"),
    # Scheduling: set the "to be processed on" date
    path("api/sales-orders/<int:pk>/scheduled/", views.api_order_set_scheduled, name="api_order_set_scheduled"),
    path("api/sales-orders/<int:pk>/refresh-conversation/", views.api_order_refresh_conversation, name="api_order_refresh_conversation"),
    path("api/dm/create-order/", views.api_n8n_create_order_from_dm, name="api_n8n_create_order_from_dm"),
    # Ads & offers: Meta spend per campaign linked to offers, cross-source revenue
    path("ads-offers/", views.ads_offers_dashboard, name="ads_offers_dashboard"),
    path("api/ads/<int:pk>/link-offer/", views.api_ad_link_offer, name="api_ad_link_offer"),
    # Exchange: get the items from the source delivered order, save return selection
    path("api/sales-orders/<int:pk>/exchange-source-items/", views.api_exchange_source_items, name="api_exchange_source_items"),
    path("api/sales-orders/<int:pk>/exchange-set-returns/", views.api_exchange_set_returns, name="api_exchange_set_returns"),
    # Admin tools (superuser only)
    path("admin-tools/", views.admin_tools, name="admin_tools"),
    path("api/admin-tools/run/<str:tool_name>/", views.api_admin_run_tool, name="api_admin_run_tool"),
    # Debug: inspect raw Navex etat response (superuser only)
    path("api/debug/navex-etat/", views.api_debug_navex_etat, name="api_debug_navex_etat"),
    # Shopify webhook: client passes order on barats.tn → auto-create v2 draft
    path("api/shopify/webhook/orders/create/", views.api_shopify_webhook_order_created, name="api_shopify_webhook_order_created"),
    # Regions / delegations (cascaded dropdown)
    path("api/regions/<int:region_id>/delegations/", views.api_region_delegations, name="api_region_delegations"),
    path("api/delegations/all/", views.api_all_delegations, name="api_all_delegations"),
    path("api/sales-pages/<int:page_id>/offers/", views.api_offers_for_page, name="api_offers_for_page"),
    path("api/offers-all/", views.api_all_offers, name="api_all_offers"),
    path("api/offers/<int:offer_id>/", views.api_offer_detail, name="api_offer_detail"),

    # V2 — Admin: manage offers
    path("admin-offers/", views.offers_manage, name="offers_manage"),
    path("changement-prix/", views.price_change_page, name="price_change_page"),
    path("api/orders/<int:pk>/set-price/", views.api_set_order_price, name="api_set_order_price"),
    path("api/offers/", views.api_offer_create, name="api_offer_create"),
    path("api/offers/<int:pk>/edit/", views.api_offer_update, name="api_offer_update"),
    path("api/offers/<int:pk>/delete/", views.api_offer_delete, name="api_offer_delete"),
]
