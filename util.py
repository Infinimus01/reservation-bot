import random
from playwright.async_api import Page
from dataclasses import dataclass
from datetime import datetime


def get_formatted_date(date_input):
    # Check if input is in YYYY-MM-DD format
    if "-" in date_input and len(date_input.split("-")) == 3:
        # Parse full date YYYY-MM-DD
        year, month, day = map(int, date_input.split("-"))
        new_date = datetime(year, month, day)
    else:
        # Legacy behavior: assume current month/year and just use the day
        today = datetime.today()
        month = today.month
        year = today.year
        new_date = datetime(year, month, int(date_input))
    return new_date.strftime("%d-%m-%Y")


# print(get_formatted_date("18"))


async def select_country_by_name(page: Page, country_name: str):
    normalized_input = country_name.lower().strip()
    options = await page.locator("#country option").all()
    best_match = None
    best_score = 0
    for option in options:
        option_text = await option.text_content()
        if not option_text:
            print("Skipping option with no text content")
            continue
        option_value = await option.get_attribute("value")
        if not option_value or option_text.strip() == "Choose a country":
            continue
        normalized_option_text = option_text.lower().strip()
        score = 0
        if normalized_option_text == normalized_input:
            score = 100

        elif normalized_input in normalized_option_text:
            score = 80

        elif normalized_option_text in normalized_input:
            score = 70

        else:
            input_words = normalized_input.split()
            option_words = normalized_option_text.split()
            matching_words = 0

            for input_word in input_words:
                for option_word in option_words:
                    if input_word in option_word or option_word in input_word:
                        matching_words += 1
                        break

            if input_words and option_words:
                score = (matching_words / max(len(input_words), len(option_words))) * 60

        if score > best_score:
            best_score = score
            best_match = {
                "option": option,
                "text": option_text.strip(),
                "value": option_value,
                "score": score,
            }

    if best_match and best_score > 30:
        print(f"Selecting: {best_match['text']} (Score: {best_match['score']})")
        await page.select_option("#country", best_match["value"], force=True)
        return best_match
    else:
        raise Exception(f'No suitable match found for "{country_name}"')


@dataclass
class UserDetails:
    unique_id: str
    date: str
    firstName: str
    lastName: str
    email: str
    phone: str
    zip: str
    country: str
    time: str
    ticket_count: int
    job_time: str
    status: str
    proxy: str
    upstream_proxy: str = ""


from faker import Faker

fake = Faker(locale="en_US")

fake.email(domain="gmail")
fake.phone_number()


def get_fake_details(seed: int) -> dict:
    fake.seed_instance(seed)
    name = fake.name().split()
    first, last = name[0], name[1]
    email = f"{first}{last}{int(random.uniform(500, 1000))}k@gmail.com".lower()
    phone = fake.numerify("##########")

    return {"email": email, "phone": phone, "firstName": first, "lastName": last}


fake_details = get_fake_details(seed=int(random.uniform(1000, 9999)))

# print(fake_details)
