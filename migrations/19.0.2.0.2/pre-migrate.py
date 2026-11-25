"""Archive retired purchase routes without touching live procurement references."""


def migrate(cr, version):
    cr.execute("""
        UPDATE stock_route
           SET active = FALSE, product_selectable = FALSE, product_categ_selectable = FALSE
         WHERE id IN (
             SELECT res_id FROM ir_model_data
              WHERE module = 'acme_workshop_fabrication'
                AND model = 'stock.route'
                AND name IN ('route_local_purchase', 'route_foreign_purchase')
         )
    """)
    cr.execute("""
        UPDATE stock_route
           SET active = TRUE, product_selectable = TRUE
         WHERE id IN (
             SELECT res_id FROM ir_model_data
              WHERE module = 'purchase_stock'
                AND model = 'stock.route' AND name = 'route_warehouse0_buy'
         )
    """)
    cr.execute("""
        WITH obsolete_views AS (
            SELECT res_id FROM ir_model_data
             WHERE module = 'acme_workshop_fabrication' AND model = 'ir.ui.view'
               AND name IN ('product_acme_purchase_routes', 'purchase_order_acme_purchase_type',
                            'purchase_request_acme_purchase_type')
        )
        DELETE FROM ir_ui_view WHERE id IN (SELECT res_id FROM obsolete_views)
    """)
    cr.execute("""
        DELETE FROM ir_model_data
         WHERE module = 'acme_workshop_fabrication' AND model = 'ir.ui.view'
           AND name IN ('product_acme_purchase_routes', 'purchase_order_acme_purchase_type',
                        'purchase_request_acme_purchase_type')
    """)
