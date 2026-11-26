
-- Order summary view 
DROP VIEW IF EXISTS v_order_summary;
CREATE VIEW v_order_summary AS
WITH item_agg AS (
    SELECT
        order_id,
        COUNT(*)                        AS item_count,
        COUNT(DISTINCT product_id)      AS distinct_products,
        ROUND(SUM(price), 2)            AS items_subtotal,
        ROUND(SUM(freight_value), 2)    AS freight_total,
        ROUND(SUM(price + freight_value), 2) AS order_total
    FROM order_item
    GROUP BY order_id
),
pay_agg AS (
    SELECT
        order_id,
        COUNT(*)                        AS payment_records,
        ROUND(SUM(payment_value), 2)    AS total_paid,
        GROUP_CONCAT(DISTINCT payment_type) AS payment_methods
    FROM payment
    GROUP BY order_id
)
SELECT
    o.order_id,
    o.customer_id,
    c.customer_unique_id,
    g.city                              AS customer_city,
    g.state                             AS customer_state,
    o.order_status,
    o.order_purchase_timestamp,
    o.order_approved_at,
    o.order_delivered_carrier_date,
    o.order_delivered_customer_date,
    o.order_estimated_delivery_date,
    COALESCE(ia.item_count, 0)          AS item_count,
    COALESCE(ia.distinct_products, 0)   AS distinct_products,
    COALESCE(ia.items_subtotal, 0.0)    AS items_subtotal,
    COALESCE(ia.freight_total, 0.0)     AS freight_total,
    COALESCE(ia.order_total, 0.0)       AS order_total,
    COALESCE(pa.total_paid, 0.0)        AS total_paid,
    pa.payment_methods
FROM orders o
LEFT JOIN customer c    ON o.customer_id = c.customer_id
LEFT JOIN geolocation g ON c.zip_code_prefix = g.zip_code_prefix
LEFT JOIN item_agg ia   ON o.order_id = ia.order_id
LEFT JOIN pay_agg pa    ON o.order_id = pa.order_id;

-- Order Status Distribution & Revenue Breakdown
SELECT
    order_status,
    COUNT(*)                            AS order_count,
    ROUND(100.0 * COUNT(*) / (SELECT COUNT(*) FROM orders), 2) AS share_pct,
    ROUND(SUM(order_total), 2)          AS total_gmv
FROM v_order_summary
GROUP BY order_status
ORDER BY order_count DESC;

-- Search Orders by Status and Date Range (Example: 'shipped' orders in 2018)
SELECT
    order_id,
    customer_id,
    customer_state,
    order_status,
    order_purchase_timestamp,
    item_count,
    order_total
FROM v_order_summary
WHERE order_status = 'shipped'
  AND order_purchase_timestamp >= '2018-01-01'
ORDER BY order_purchase_timestamp DESC
LIMIT 20;

-- Search All Orders Placed by a Specific Customer (customer_id or customer_unique_id)
SELECT
    order_id,
    customer_id,
    customer_unique_id,
    order_status,
    order_purchase_timestamp,
    item_count,
    order_total,
    payment_methods
FROM v_order_summary
WHERE customer_id = 'f3f0e613e0bdb9c7cee75504f0f90679'
ORDER BY order_purchase_timestamp DESC;

-- Order Detail - Header & Lifecycle Timeline (Example order_id)
SELECT
    order_id,
    customer_id,
    customer_unique_id,
    customer_city,
    customer_state,
    order_status,
    order_purchase_timestamp,
    order_approved_at,
    order_delivered_carrier_date,
    order_delivered_customer_date,
    order_estimated_delivery_date,
    items_subtotal,
    freight_total,
    order_total,
    total_paid,
    payment_methods
FROM v_order_summary
WHERE order_id = '0008288aa423d2a3f00fcb17cd7d8719';

-- Order Detail - Line Items with Product Category & Seller Location
SELECT
    oi.order_id,
    oi.order_item_id,
    oi.product_id,
    COALESCE(pc.product_category_name_english, p.product_category_name, 'unknown') AS category_en,
    oi.seller_id,
    sg.city                             AS seller_city,
    sg.state                            AS seller_state,
    oi.shipping_limit_date,
    oi.price,
    oi.freight_value,
    ROUND(oi.price + oi.freight_value, 2) AS line_total
FROM order_item oi
LEFT JOIN product p           ON oi.product_id = p.product_id
LEFT JOIN product_category pc ON p.product_category_name = pc.product_category_name
LEFT JOIN seller s            ON oi.seller_id = s.seller_id
LEFT JOIN geolocation sg      ON s.zip_code_prefix = sg.zip_code_prefix
WHERE oi.order_id = '0008288aa423d2a3f00fcb17cd7d8719'
ORDER BY oi.order_item_id;

-- Order Detail - Payment & Review Records
SELECT
    payment_sequential,
    payment_type,
    payment_installments,
    payment_value
FROM payment
WHERE order_id = '0008288aa423d2a3f00fcb17cd7d8719'
ORDER BY payment_sequential;

SELECT
    review_pk,
    review_id,
    review_score,
    review_comment_title,
    review_comment_message,
    review_creation_date,
    review_answer_timestamp
FROM review
WHERE order_id = '0008288aa423d2a3f00fcb17cd7d8719';

-- Delivery SLA & Delayed Orders Tracking (Delivered orders vs. Estimated date)
SELECT
    COUNT(*) AS delivered_orders,
    SUM(CASE WHEN DATE(order_delivered_customer_date) <= DATE(order_estimated_delivery_date) THEN 1 ELSE 0 END) AS on_time_orders,
    SUM(CASE WHEN DATE(order_delivered_customer_date) >  DATE(order_estimated_delivery_date) THEN 1 ELSE 0 END) AS late_orders,
    ROUND(100.0 * SUM(CASE WHEN DATE(order_delivered_customer_date) <= DATE(order_estimated_delivery_date) THEN 1 ELSE 0 END) / COUNT(*), 2) AS on_time_rate_pct,
    ROUND(AVG(JULIANDAY(order_delivered_customer_date) - JULIANDAY(order_purchase_timestamp)), 2) AS avg_delivery_days
FROM orders
WHERE order_status = 'delivered'
  AND order_delivered_customer_date IS NOT NULL
  AND order_estimated_delivery_date IS NOT NULL;
