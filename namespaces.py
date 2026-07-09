from rdflib import Graph, Namespace
from rdflib.namespace import XSD

UHI = Namespace("https://w3id.org/stuttgart-uhi#")
BOT = Namespace("https://w3id.org/bot#")
SOSA = Namespace("http://www.w3.org/ns/sosa/")
GEO = Namespace("http://www.opengis.net/ont/geosparql#")
ALKIS = Namespace("https://w3id.org/stuttgart-uhi/alkis/")
EX = Namespace("https://w3id.org/stuttgart-uhi/data/")


def bind_all(g: Graph) -> None:
    for prefix, ns in [
        ("uhi", UHI),
        ("bot", BOT),
        ("sosa", SOSA),
        ("geo", GEO),
        ("alkis", ALKIS),
        ("ex", EX),
        ("xsd", XSD),
    ]:
        g.bind(prefix, ns)
        