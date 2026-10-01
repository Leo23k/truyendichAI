from app.crawlers.plugins.shuba69 import crawler as shuba69_crawler
from app.crawlers.plugins.fanqie import crawler as fanqie_crawler
from app.crawlers.plugins.text_sites import piaotian_crawler, uukanshu_crawler
from app.crawlers.plugins.trxs import crawler as trxs_crawler
from app.crawlers.plugins.xsw import crawler as xsw_crawler
from app.crawlers.plugins.hjwzw import crawler as hjwzw_crawler
from app.crawlers.plugins.tienhiep import crawler as tienhiep_crawler
from app.crawlers.registry import registry

registry.register(shuba69_crawler)
registry.register(fanqie_crawler)
registry.register(piaotian_crawler)
registry.register(uukanshu_crawler)
registry.register(trxs_crawler)
registry.register(xsw_crawler)
registry.register(hjwzw_crawler)
registry.register(tienhiep_crawler)
