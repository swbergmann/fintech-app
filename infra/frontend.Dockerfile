FROM nginx:stable-alpine
ENV BACKEND_UPSTREAM=backend:8080
ENV NGINX_ENVSUBST_FILTER=^BACKEND_UPSTREAM$
COPY frontend/ /usr/share/nginx/html/
COPY infra/nginx.conf /etc/nginx/templates/default.conf.template
COPY infra/15-validate-upstream.sh /docker-entrypoint.d/15-validate-upstream.sh
RUN chmod +x /docker-entrypoint.d/15-validate-upstream.sh
