FROM opensearchproject/opensearch:3.9.0

# Japanese morphological analysis + ICU normalization.
# Drop the heavy plugins this knowledge store does not use so a 512m heap fits on a ~4GB RAM host.
RUN set -eux; \
    /usr/share/opensearch/bin/opensearch-plugin install --batch analysis-kuromoji analysis-icu; \
    for p in opensearch-skills opensearch-neural-search opensearch-knn opensearch-ml opensearch-performance-analyzer; do \
      /usr/share/opensearch/bin/opensearch-plugin remove "$p" || echo "skip $p"; \
    done; \
    /usr/share/opensearch/bin/opensearch-plugin list

COPY entrypoint.sh /usr/local/bin/ks-entrypoint.sh
USER root
RUN chmod 755 /usr/local/bin/ks-entrypoint.sh
USER opensearch

ENTRYPOINT ["/usr/local/bin/ks-entrypoint.sh"]
