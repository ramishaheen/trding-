# Freqtrade image + psycopg, so MyStrategy can read the LLM `market_context`
# table (the risk-off / news-pause gate). Without this the AI gate silently
# fails open ("No module named 'psycopg'") and never affects trading.
FROM freqtradeorg/freqtrade:stable

USER root
RUN pip install --no-cache-dir "psycopg[binary]>=3.1"
USER ftuser
