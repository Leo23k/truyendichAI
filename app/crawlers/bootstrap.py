from app.crawlers.plugins.shuba69 import crawler as shuba69_crawler
from app.crawlers.plugins.trxs import crawler as trxs_crawler
from app.crawlers.plugins.xsw import crawler as xsw_crawler
from app.crawlers.registry import registry

registry.register(shuba69_crawler)
registry.register(trxs_crawler)
registry.register(xsw_crawler)
