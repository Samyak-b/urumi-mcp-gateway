#!/bin/sh
# Store setup, run by a Helm post-install/post-upgrade hook Job (templates/setup-job.yaml).
# Idempotent: every step checks the current state first or writes the same value again, so the Job can
# run on every upgrade and after a failure.
set -eu
cd /var/www/html
log() { echo "[setup] $*"; }
wp() { command wp --user="$WP_ADMIN_USER" "$@"; }

# The WordPress pod copies its files into the shared volume on first start; wait for that.
log "waiting for WordPress files on the shared volume"
i=0
until [ -f wp-config.php ] && [ -f wp-includes/version.php ]; do
  i=$((i + 1)); [ "$i" -gt 120 ] && { log "gave up waiting for WordPress files"; exit 1; }
  sleep 5
done

log "waiting for the database"
until php -r '
  $h = explode(":", getenv("WORDPRESS_DB_HOST"));
  try { new mysqli($h[0], getenv("WORDPRESS_DB_USER"), getenv("WORDPRESS_DB_PASSWORD"), getenv("WORDPRESS_DB_NAME"), (int)($h[1] ?? 3306)); exit(0); }
  catch (Throwable $e) { exit(1); }'; do
  sleep 5
done

if ! command wp core is-installed 2>/dev/null; then
  log "installing WordPress"
  command wp core install --url="$WP_HOME_URL" --title="$STORE_TITLE" \
    --admin_user="$WP_ADMIN_USER" --admin_password="$WP_ADMIN_PASSWORD" \
    --admin_email="$WP_ADMIN_EMAIL" --skip-email
fi
# Keep the URL and admin password in line with the values and the Secret on every run.
wp option update home "$WP_HOME_URL"
wp option update siteurl "$WP_HOME_URL"
wp user update "$WP_ADMIN_USER" --user_pass="$WP_ADMIN_PASSWORD" --skip-email >/dev/null
wp option update timezone_string "$STORE_TIMEZONE"
wp rewrite structure '/%postname%/'

if ! wp plugin is-installed woocommerce; then
  log "installing WooCommerce ${WC_VERSION:-latest}"
  if [ -n "${WC_VERSION:-}" ]; then wp plugin install woocommerce --version="$WC_VERSION"; else wp plugin install woocommerce; fi
fi
wp plugin is-active woocommerce || wp plugin activate woocommerce
wp theme is-installed storefront || wp theme install storefront
wp theme activate storefront

log "configuring WooCommerce for a test store"
wp option update woocommerce_coming_soon no           # new stores start behind a "coming soon" page
wp option update woocommerce_store_pages_only no
wp option update woocommerce_currency "$STORE_CURRENCY"
wp option update woocommerce_default_country "US:CA"
wp option update woocommerce_ship_to_countries disabled   # no shipping zones needed to check out
wp option update woocommerce_calc_taxes no
# Cosmetic: hides the onboarding checklist. Recent WooCommerce versions may refuse these writes, so they
# must never fail the setup.
wp option update woocommerce_task_list_hidden yes || log "could not hide the onboarding task list (cosmetic)"
wp option update woocommerce_onboarding_profile '{"skipped":true,"completed":true}' --format=json || log "could not skip onboarding (cosmetic)"
# Cash on delivery: the test-friendly payment method the assignment allows.
wp option update woocommerce_cod_settings '{"enabled":"yes","title":"Cash on delivery","description":"Pay with cash upon delivery.","instructions":"Pay with cash upon delivery.","enable_for_methods":[],"enable_for_virtual":"yes"}' --format=json
wp wc tool run install_pages >/dev/null || log "install_pages failed (pages may already exist)"
# The home page is the shop, not WordPress's "Hello world" post.
wp option update show_on_front page
wp option update page_on_front "$(wp option get woocommerce_shop_page_id)"
wp rewrite flush

if [ "${SAMPLE_PRODUCTS:-true}" = "true" ] && [ "$(wp post list --post_type=product --format=count)" = "0" ]; then
  log "creating sample products"
  wp wc product create --name="Canvas Tote Bag" --regular_price="18.00" --status=publish >/dev/null
  wp wc product create --name="Ceramic Pour-over Set" --regular_price="42.00" --status=publish >/dev/null
  wp wc product create --name="Single-origin Coffee Beans 250g" --regular_price="14.50" --status=publish >/dev/null
fi

log "done: storefront at $WP_HOME_URL"
