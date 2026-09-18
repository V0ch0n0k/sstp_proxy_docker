FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    squid \
    sstp-client \
    ppp \
    ca-certificates \
    iptables \
    iproute2 \
    net-tools \
    curl \
 && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /home/scripts
COPY ./src/sstp_starter.sh /home/scripts/sstp_starter.sh
# Страховка от CRLF, если репозиторий склонирован на Windows с autocrlf
RUN sed -i 's/\r$//' /home/scripts/sstp_starter.sh \
 && chmod +x /home/scripts/sstp_starter.sh

EXPOSE 3128
CMD ["/home/scripts/sstp_starter.sh"]
