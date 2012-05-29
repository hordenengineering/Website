from main import app, db, gocardless, mail
from models.user import User, PasswordReset
from models.payment import Payment, BankPayment, GoCardlessPayment
from models.ticket import TicketType, Ticket

from flask import \
    render_template, redirect, request, flash, \
    url_for, abort, send_from_directory, session
from flaskext.login import \
    login_user, login_required, logout_user, current_user
from flaskext.mail import Message
from flaskext.wtf import \
    Form, Required, Email, EqualTo, ValidationError, \
    TextField, PasswordField, SelectField, SubmitField

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import NoResultFound

from decorator import decorator
import simplejson, os, re
from datetime import datetime, timedelta

def feature_flag(flag):
    def call(f, *args, **kw):
        if app.config.get(flag, False) == True:
            return f(*args, **kw)
        return abort(404)
    return decorator(call)

class IntegerSelectField(SelectField):
    def __init__(self, *args, **kwargs):
        kwargs['coerce'] = int
        self.fmt = kwargs.pop('fmt', str)
        self.values = kwargs.pop('values', [])
        SelectField.__init__(self, *args, **kwargs)

    @property
    def values(self):
        return self._values

    @values.setter
    def values(self, vals):
        self._values = vals
        self.choices = [(i, self.fmt(i)) for i in vals]


@app.route("/")
def main():
    return render_template('main.html')

@app.route('/favicon.ico')
def favicon():
    return send_from_directory(os.path.join(app.root_path, 'static/images'),
                                   'favicon.ico', mimetype='image/vnd.microsoft.icon')

@app.route("/sponsors")
def sponsors():
    return render_template('sponsors.html')


@app.route("/about/company")
def company():
    return render_template('company.html')


class NextURLField(TextField):
    def _value(self):
        # Cheap way of ensuring we don't get absolute URLs
        if not self.data or '//' in self.data:
            return ''
        if not re.match('^[-a-z/?=&]+$', self.data):
            return ''
        return self.data

class LoginForm(Form):
    email = TextField('Email', [Email(), Required()])
    password = PasswordField('Password', [Required()])
    next = NextURLField('Next')

@app.route("/login", methods=['GET', 'POST'])
@feature_flag('PAYMENTS')
def login():
    form = LoginForm(request.form, next=request.args.get('next'))
    if request.method == 'POST' and form.validate():
        user = User.query.filter_by(email=form.email.data).first()
        if user and user.check_password(form.password.data):
            login_user(user)
            return redirect(form.next.data or url_for('tickets'))
        else:
            flash("Invalid login details!")
    return render_template("login.html", form=form)

class SignupForm(Form):
    name = TextField('Name', [Required()])
    email = TextField('Email', [Email(), Required()])
    password = PasswordField('Password', [Required(), EqualTo('confirm', message='Passwords do not match')])
    confirm = PasswordField('Confirm password', [Required()])
    next = NextURLField('Next')

@app.route("/signup", methods=['GET', 'POST'])
@feature_flag('PAYMENTS')
def signup():
    form = SignupForm(request.form, next=request.args.get('next'))

    if request.method == 'POST' and form.validate():
        user = User(form.email.data, form.name.data)
        user.set_password(form.password.data)
        db.session.add(user)
        try:
            db.session.commit()
        except IntegrityError, e:
            raise
        login_user(user)
        return redirect(form.next.data or url_for('tickets'))

    return render_template("signup.html", form=form)

class ForgotPasswordForm(Form):
    email = TextField('Email', [Email(), Required()])

    def validate_email(form, field):
        user = User.query.filter_by(email=form.email.data).first()
        if not user:
            raise ValidationError('Email address not found')
        form._user = user

@app.route("/forgot-password", methods=['GET', 'POST'])
@feature_flag('PAYMENTS')
def forgot_password():
    form = ForgotPasswordForm(request.form)
    if request.method == 'POST' and form.validate():
        if form._user:
            reset = PasswordReset(form.email.data)
            reset.new_token()
            db.session.add(reset)
            db.session.commit()
            msg = Message("EMF Camp password reset",
                sender=("EMF Camp 2012", app.config.get('EMAIL')),
                recipients=[form.email.data])
            msg.body = render_template("reset-password-email.txt", user=form._user, reset=reset)
            mail.send(msg)

        return redirect(url_for('reset_password', email=form.email.data))
    return render_template("forgot-password.html", form=form)

class ResetPasswordForm(Form):
    email = TextField('Email', [Email(), Required()])
    token = TextField('Token', [Required()])
    password = PasswordField('New password', [Required(), EqualTo('confirm', message='Passwords do not match')])
    confirm = PasswordField('Confirm password', [Required()])

    def validate_token(form, field):
        reset = PasswordReset.query.filter_by(email=form.email.data, token=field.data).first()
        if not reset:
            raise ValidationError('Token not found')
        if reset.expired():
            raise ValidationError('Token has expired')
        form._reset = reset

@app.route("/reset-password", methods=['GET', 'POST'])
@feature_flag('PAYMENTS')
def reset_password():
    form = ResetPasswordForm(request.form, email=request.args.get('email'), token=request.args.get('token'))
    if request.method == 'POST' and form.validate():
        user = User.query.filter_by(email=form.email.data).first()
        db.session.delete(form._reset)
        user.set_password(form.password.data)
        db.session.add(user)
        db.session.commit()
        return redirect(url_for('tickets'))
    return render_template("reset-password.html", form=form)

@app.route("/logout")
@feature_flag('PAYMENTS')
@login_required
def logout():
    logout_user()
    return redirect('/')


class ChoosePrepayTicketsForm(Form):
    count = IntegerSelectField('Number of tickets', [Required()])
    provider = TextField('Provider')
    pay = SubmitField('Pay')
    choose = SubmitField('Choose')

    def validate_provider(form, field):
        if not field.data:
            return None
        if field.data not in ('gocardless', 'banktransfer'):
            raise ValidationError('Unknown provider %s' % field.data)

    def validate_count(form, field):
        paid = current_user.tickets.filter_by(type=TicketType.Prepay).count()
        if field.data + paid > TicketType.Prepay.limit:
            raise ValidationError('You can only buy %s tickets' % TicketType.Prepay.limit)

@app.route("/tickets", methods=['GET', 'POST'])
@feature_flag('PAYMENTS')
@login_required
def tickets():
    form = ChoosePrepayTicketsForm(request.form, provider=request.args.get('provider'))
    form.count.values = range(1, TicketType.Prepay.limit + 1)

    if request.method == 'POST' and form.validate():
        for i in range(form.count.data):
            t = Ticket(type_id=TicketType.Prepay.id)
            current_user.tickets.append(t)

        db.session.add(current_user)
        db.session.commit()

        if form.pay.data:
            return redirect(url_for('pay_choose', provider=form.provider.data))

        return redirect(url_for('pay_choose'))


    tickets = current_user.tickets.all()
    payments = current_user.payments.all()

    return render_template("tickets.html",
        form=form,
        tickets=tickets,
        payments=payments,
        amount=1,
        total=TicketType.Prepay.cost,
    )

def buy_prepay_tickets(paymenttype):
    """
    Temporary procedure to create a payment for all outstanding tickets
    """

    tickets = current_user.tickets.filter_by(payment_id=None)
    amount = sum(t.type.cost for t in tickets)

    if not amount:
        raise Exception('There are no tickets without payments')

    payment = paymenttype(amount)
    current_user.payments.append(payment)

    for t in tickets:
        t.payment = payment
        t.expires = datetime.utcnow() + timedelta(days=app.config.get('EXPIRY_DAYS'))

    db.session.add(current_user)
    db.session.commit()

    return payment


@app.route("/pay")
@feature_flag('PAYMENTS')
def pay():
    if current_user.is_authenticated():
        return redirect(url_for('pay_choose'))

    return render_template('payment-choose.html', next='tickets')

@app.route("/pay/choose")
@feature_flag('PAYMENTS')
def pay_choose():
    provider = request.args.get('provider')
    if provider == 'gocardless':
        return redirect(url_for('gocardless_start'))
    elif provider == 'banktransfer':
        return redirect(url_for('transfer_start'))

    return render_template('payment-choose.html', next='pay_choose')

@app.route("/pay/terms")
@feature_flag('PAYMENTS')
def ticket_terms():
    return render_template('terms.html')


@app.route("/pay/gocardless-start")
@feature_flag('PAYMENTS')
@login_required
def gocardless_start():
    payment = buy_prepay_tickets(GoCardlessPayment)

    app.logger.info("User %s created GoCardless payment %s", current_user.id, payment.id)

    bill_url = payment.bill_url("Electromagnetic Field Ticket Deposit")

    return redirect(bill_url)


@app.route("/pay/gocardless-complete")
@feature_flag('PAYMENTS')
@login_required
def gocardless_complete():
    payment_id = int(request.args.get('payment'))

    app.logger.info("gocardless-complete: userid %s, payment_id %s, gcid %s",
        current_user.id, payment_id, request.args.get('resource_id'))

    try:
        gocardless.client.confirm_resource(request.args)

        if request.args["resource_type"] != "bill":
            raise ValueError("GoCardless resource type %s, not bill" % request.args['resource_type'])

        gcid = request.args["resource_id"]

        payment = current_user.payments.filter_by(id=payment_id).one()

    except Exception, e:
        app.logger.error("gocardless-complete exception: %s", e)
        flash("An error occurred with your payment, please contact %s" % app.config.get('TICKETS_EMAIL'))
        return redirect(url_for('tickets'))

    # keep the gocardless reference so we can find the payment when we get called by the webhook
    payment.gcid = gcid
    payment.state = "inprogress"
    db.session.add(payment)
    db.session.commit()

    app.logger.info("Payment completed OK")

    # TODO send an email with the details.
    # should we send the resource_uri in the bill email?

    return redirect(url_for('gocardless_waiting', payment=payment_id))

@app.route('/pay/gocardless-waiting')
@feature_flag('PAYMENTS')
@login_required
def gocardless_waiting():
    payment_id = int(request.args.get('payment'))
    payment = current_user.payments.filter_by(id=payment_id).one()
    return render_template('gocardless-waiting.html', payment=payment, days=app.config.get('EXPIRY_DAYS'))

@app.route("/pay/gocardless-cancel")
@feature_flag('PAYMENTS')
@login_required
def gocardless_cancel():
    payment_id = int(request.args.get('payment'))

    app.logger.info("gocardless-cancel: userid %s, payment_id %s",
        current_user.id, payment_id)

    try:
        payment = current_user.payments.filter_by(id=payment_id).one()

    except Exception, e:
        app.logger.error("gocardless-cancel exception: %s", e)
        flash("An error occurred with your payment, please contact %s" % app.config.get('TICKETS_EMAIL'))
        return redirect(url_for('tickets'))

    for t in payment.tickets:
        t.payment = None

    db.session.add(current_user)
    db.session.commit()

    # TODO send an email with the details.
    # should we send the resource_uri in the bill email?

    app.logger.info("Payment completed OK")

    return render_template('gocardless-cancel.html', payment=payment)

@app.route("/gocardless-webhook", methods=['POST'])
@feature_flag('PAYMENTS')
def gocardless_webhook():
    """
        handle the gocardless webhook / callback callback:
        https://gocardless.com/docs/web_hooks_guide#response
        
        we mostly want 'bill'
        
        GoCardless limits the webhook to 5 secs. this should run async...

    """
    json_data = simplejson.loads(request.data)
    data = json_data['payload']

    if not gocardless.client.validate_webhook(data):
        return ('', 403)

    app.logger.info("gocardless-webhook: %s %s", data.get('resource_type'), data.get('action'))

    if data['resource_type'] != 'bill':
        app.logger.warn('Resource type is not bill')
        return ('', 501)

    if data['action'] not in ['paid', 'withdrawn', 'failed', 'created']:
        app.logger.warn('Unknown action')
        return ('', 501)

    # action can be:
    #
    # paid -> money taken from the customers account, at this point we concider the ticket paid.
    # created -> for subscriptions
    # failed -> customer is broke
    # withdrawn -> we actually get the money

    for bill in data['bills']:
        gcid = bill['id']
        try:
            payment = GoCardlessPayment.query.filter_by(gcid=gcid).one()
        except NoResultFound:
            app.logger.warn('Payment %s not found, ignoring', gcid)
            continue

        app.logger.info("Processing payment %s (%s) for user %s",
            payment.id, gcid, payment.user.id)

        if data['action'] == 'paid':
            if payment.state != "inprogress":
                app.logger.warning("Old payment state was %s, not 'inprogress'", payment.state)

            for t in payment.tickets.all():
                t.paid = True

            payment.state = "paid"
            db.session.add(payment)
            db.session.commit()

            # TODO email the user

        else:
            app.logger.debug('Payment: %s', bill)

    return ('', 200)


@app.route("/pay/transfer-start")
@feature_flag('PAYMENTS')
@login_required
def transfer_start():
    payment = buy_prepay_tickets(BankPayment)

    # XXX TODO send an email with the details.

    app.logger.info("User %s created bank payment %s (%s)", current_user.id, payment.id, payment.bankref)

    payment.state = "inprogress"
    db.session.add(payment)
    db.session.commit()

    return redirect(url_for('transfer_waiting', payment=payment.id))

@app.route("/pay/transfer-waiting")
@feature_flag('PAYMENTS')
@login_required
def transfer_waiting():
    payment_id = int(request.args.get('payment'))
    payment = current_user.payments.filter_by(id=payment_id, user=current_user).one()
    return render_template('transfer-waiting.html', payment=payment, days=app.config.get('EXPIRY_DAYS'))

