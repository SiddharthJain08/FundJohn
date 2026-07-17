from strategies.implementations.oxf_nr7 import OxfNR7
def test_id_and_adaptation_note():
    s = OxfNR7()
    assert s.id == 'oxf_nr7'
    assert 'adaptation' in s.description.lower()  # honesty: documented as adaptation
