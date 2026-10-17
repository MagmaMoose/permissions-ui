from permissions_ui.directory import User, person_name


def test_names_stop_at_the_surname():
    assert person_name("Jane Doe - Contoso", "Jane", "Doe") == "Jane Doe"
    assert person_name("Jan van der Berg (Fabrikam)", "jan", "Van der Berg") == "Jan van der Berg"
    assert person_name("Ann Lee - Smith - Contoso", "Ann", "Lee - Smith") == "Ann Lee - Smith"
    assert person_name("Tom Baker - Baker Ltd", "Tom", "Baker") == "Tom Baker"
    assert person_name("Doe, Jane (Contoso)", "Jane", "Doe") == "Doe, Jane (Contoso)"
    assert person_name("Jane Doe - Contoso", "", "") == "Jane Doe - Contoso"
    assert User.parse({"displayName": "Jane Doe - Contoso", "givenName": "Jane", "surname": "Doe", "userPrincipalName": "Jane@Example.com"}).name == "Jane Doe"
