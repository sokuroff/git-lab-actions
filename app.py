import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse
import atexit

from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_restx import Api, Resource, fields
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///price_tracker.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
# Убираем стандартное сообщение об ошибке restx, чтобы видеть ошибки Flask/SQLAlchemy
app.config['ERROR_404_HELP'] = False

db = SQLAlchemy(app)
api = Api(app, version='1.0', title='Price Tracker API',
          description='An API to track product prices from URLs')

ns = api.namespace('products', description='Product operations')


class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    url = db.Column(db.String(512), unique=True, nullable=False)
    name = db.Column(db.String(255), nullable=False)
    domain = db.Column(db.String(100))
    # Связь с историей цен. `lazy=True` означает, что цены будут загружаться при обращении.
    prices = db.relationship('PriceHistory', backref='product', lazy=True, cascade="all, delete-orphan")

class PriceHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    price = db.Column(db.Float, nullable=False)
    timestamp = db.Column(db.DateTime, server_default=db.func.now())
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)


# --- МОДЕЛИ API (Flask-RESTX) ---
# Для вывода истории цен
price_history_model = api.model('PriceHistory', {
    'price': fields.Float,
    'timestamp': fields.DateTime
})

# Для вывода полного описания продукта
product_model = api.model('Product', {
    'id': fields.Integer(readonly=True),
    'name': fields.String,
    'url': fields.String,
    'domain': fields.String,
    'prices': fields.List(fields.Nested(price_history_model))  # Вложенный список цен
})

# Для добавления нового продукта (только URL)
product_input_model = api.model('ProductInput', {
    'url': fields.String(required=True, description='URL of the product page')
})

SITE_SELECTORS = {
    'www.ozon.ru': ('#layoutPage > div.b6 > div.container.c > div.lq0_27.ql3_27.ql5_27 > div.m4v_27 > div > div > div.lq0_27.ql6_27.ql3_27.lq4_27 > div.rm1_27.r3m_27 > div > div.r1m_27 > div > div > div.m0o_27 > div.m7n_27.a201-a.a201-a3 > button > span > div > div.km0_27.mk0_27 > div > div > span'),
    # Добавьте сюда другие сайты, которые хотите поддерживать
}


def scrape_product_data(url):
    """
    Парсит страницу товара, извлекая название и цену.
    Возвращает (название, цена) или (None, None) в случае неудачи.
    """
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()  # Вызовет ошибку, если статус не 200 OK

        soup = BeautifulSoup(response.text, 'html.parser')
        domain = urlparse(url).netloc

        if domain not in SITE_SELECTORS:
            raise ValueError(f"Domain {domain} is not supported.")

        price_selector, name_selector = SITE_SELECTORS[domain]

        # Ищем элементы
        name_element = soup.select_one(name_selector)
        price_element = soup.select_one(price_selector)

        if not name_element or not price_element:
            return None, None

        # Очищаем данные
        name = name_element.get_text(strip=True)
        price_text = price_element.get_text(strip=True).replace(' ', '').replace('₽', '').replace(',', '.')
        price = float(''.join(filter(str.isdigit or str.isclose, price_text)))

        return name, price
    except (requests.RequestException, ValueError, TypeError) as e:
        print(f"Error scraping {url}: {e}")
        return None, None


@ns.route('/')
class ProductList(Resource):
    @ns.doc('list_products')
    @ns.marshal_list_with(product_model)
    def get(self):
        """List all tracked products"""
        return Product.query.all()

    @ns.doc('create_product')
    @ns.expect(product_input_model)
    @ns.marshal_with(product_model, code=201)
    def post(self):
        """Add a new product to track"""
        url = api.payload['url']
        if Product.query.filter_by(url=url).first():
            api.abort(409, f"Product with URL {url} is already being tracked.")

        name, price = scrape_product_data(url)

        if name is None or price is None:
            api.abort(400, f"Could not scrape data from URL. Check if the domain is supported and the page is correct.")

        # Создаем продукт и его первую запись о цене
        new_product = Product(
            url=url,
            name=name,
            domain=urlparse(url).netloc
        )
        db.session.add(new_product)
        # Важно! Сначала нужно сохранить продукт, чтобы он получил ID
        db.session.flush()

        new_price = PriceHistory(price=price, product_id=new_product.id)
        db.session.add(new_price)

        db.session.commit()
        return new_product, 201


@ns.route('/<int:id>')
@ns.response(404, 'Product not found')
@ns.param('id', 'The product identifier')
class ProductResource(Resource):
    @ns.doc('get_product')
    @ns.marshal_with(product_model)
    def get(self, id):
        """Fetch a given product, including its price history"""
        return Product.query.get_or_404(id)

    @ns.doc('delete_product')
    @ns.response(204, 'Product deleted')
    def delete(self, id):
        """Delete a product from tracking"""
        product = Product.query.get_or_404(id)
        db.session.delete(product)
        db.session.commit()
        return '', 204


def update_prices_job():
    """Задача, которая обновляет цены для всех товаров в БД."""
    print("Running scheduled price update...")
    with app.app_context():  # Важно: даем задаче доступ к контексту приложения Flask
        products = Product.query.all()
        for product in products:
            _, price = scrape_product_data(product.url)
            if price is not None:
                # Проверяем, отличается ли новая цена от последней сохраненной
                last_price_entry = PriceHistory.query.filter_by(product_id=product.id).order_by(PriceHistory.timestamp.desc()).first()
                if last_price_entry is None or last_price_entry.price != price:
                    new_price = PriceHistory(price=price, product_id=product.id)
                    db.session.add(new_price)
                    print(f"Updated price for {product.name} to {price}")
        db.session.commit()
    print("Price update finished.")


scheduler = BackgroundScheduler()
# Запускать задачу каждые 4 часа
scheduler.add_job(func=update_prices_job, trigger="interval", hours=4)
scheduler.start()

# Корректно завершаем работу планировщика при выходе из приложения
atexit.register(lambda: scheduler.shutdown())

# --- ЗАПУСК ПРИЛОЖЕНИЯ ---

if __name__ == '__main__':
    with app.app_context():
        db.create_all()  # Создаем таблицы, если их нет
    app.run(debug=True, use_reloader=False)  # use_reloader=False важно, чтобы планировщик не запускался дважды
