# Бэкенд: S3-совместимое хранилище (AWS S3 / MinIO / Backblaze / Wasabi / Selectel / …).
# Защита от удаления (write-once) — через Object Lock бакета, выставляется ОДИН РАЗ
# на уровне бакета (см. install-collector.sh / README). Аплоадер просто кладёт объекты.
# Требуемые переменные в archive.conf: S3_ENDPOINT, S3_BUCKET, S3_ENV(файл с ключами), S3_REGION.
_awsc(){
  docker run --rm -i --env-file "$S3_ENV" \
    -v /etc/ssl/certs/ca-certificates.crt:/ca.crt:ro -e AWS_CA_BUNDLE=/ca.crt \
    amazon/aws-cli@sha256:33a16c53ea442e918b58df67301ad76a8fb57082c39f7e78753b898c3f3e87c2 --endpoint-url "$S3_ENDPOINT" "$@"
}
backend_prepare(){
  : "${S3_ENDPOINT:?}" "${S3_BUCKET:?}" "${S3_ENV:?}"
  [ -f "$S3_ENV" ] || { echo "нет файла ключей $S3_ENV"; return 1; }
}
backend_put(){ # <local_file> <relpath>
  local file="$1" key="$2"
  cat "$file" | _awsc s3 cp - "s3://$S3_BUCKET/$key" >/dev/null 2>&1
}
backend_retention(){ : ; }   # retention/иммутабельность на уровне бакета, не здесь
