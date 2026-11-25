import math
from odoo import api, fields, models, Command, _
from odoo.exceptions import UserError, ValidationError

_FAB_TOKEN = object()
ENCLOSURES = [('partial', 'Partially enclosed'), ('full', 'Fully enclosed'), ('none', 'Not required')]


class FabricationRequest(models.Model):
    _name = 'acme.fabrication.request'
    _description = 'Fabrication Work Order Request'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'id desc'
    _check_company_auto = True

    name = fields.Char(default='New', readonly=True, copy=False)
    title = fields.Char(string='Work Order', required=True)
    project_id = fields.Many2one('project.project', required=True, ondelete='restrict')
    company_id = fields.Many2one(related='project_id.company_id', store=True)
    customer_id = fields.Many2one(related='project_id.partner_id', string='Customer')
    project_code = fields.Char(related='project_id.acme_contract_reference')
    order_date = fields.Date(default=fields.Date.context_today, required=True)
    description = fields.Text(string='Brief Description of Work', required=True)
    completion_date = fields.Date(string='Required Completion Date', required=True)
    prototype = fields.Selection([('yes', 'Yes'), ('no', 'No')], string='Sample / Prototype', default='no', required=True)
    drawing_status = fields.Selection(ENCLOSURES, default='none', required=True)
    drawing_pages = fields.Integer()
    boq_status = fields.Selection(ENCLOSURES, default='none', required=True)
    boq_pages = fields.Integer()
    instruction_status = fields.Selection(ENCLOSURES, string='Engineering Instruction / Critical Specification', default='none', required=True)
    instruction_pages = fields.Integer()
    line_ids = fields.One2many('acme.fabrication.line', 'request_id', string='Requested Products / BoMs', copy=True)
    state = fields.Selection([('draft', 'Draft'), ('submitted', 'Submitted'), ('approved', 'Approved'), ('received', 'Received by Workshop'), ('reported', 'Completion Reported'), ('accepted', 'Accepted'), ('rejected', 'Rejected'), ('cancel', 'Cancelled')], default='draft', required=True, readonly=True, copy=False, tracking=True)
    approver_id = fields.Many2one('res.users', string='Authorized Approver', domain="[('share', '=', False)]")
    receiver_id = fields.Many2one('res.users', string='Workshop Receiver', domain="[('share', '=', False)]")
    acceptor_id = fields.Many2one('res.users', string='Completion Acceptor', domain="[('share', '=', False)]")
    prepared_by_id = fields.Many2one('res.users', readonly=True, copy=False)
    prepared_at = fields.Datetime(readonly=True, copy=False)
    approved_by_id = fields.Many2one('res.users', readonly=True, copy=False)
    approved_at = fields.Datetime(readonly=True, copy=False)
    received_by_id = fields.Many2one('res.users', readonly=True, copy=False)
    received_at = fields.Datetime(readonly=True, copy=False)
    reported_by_id = fields.Many2one('res.users', readonly=True, copy=False)
    reported_at = fields.Datetime(readonly=True, copy=False)
    accepted_by_id = fields.Many2one('res.users', readonly=True, copy=False)
    accepted_at = fields.Datetime(readonly=True, copy=False)
    decision_note = fields.Text(string='Decision / Rejection Note')
    review_activity_id = fields.Many2one('mail.activity', readonly=True, copy=False, ondelete='set null')
    production_ids = fields.One2many('mrp.production', 'acme_fabrication_request_id', string='Manufacturing Orders', copy=False)
    actual_start = fields.Datetime(compute='_compute_production_dates', store=True, compute_sudo=True, string='Manufacturing Started')
    actual_finish = fields.Datetime(compute='_compute_production_dates', store=True, compute_sudo=True, string='Manufacturing Finished')

    @api.depends('production_ids.state', 'production_ids.date_start', 'production_ids.date_finished', 'line_ids')
    def _compute_production_dates(self):
        for request in self:
            active = request.production_ids.filtered(lambda mo: mo.state != 'cancel')
            started = active.filtered(lambda mo: mo.state in ('progress', 'to_close', 'done'))
            starts = started.mapped('date_start')
            request.actual_start = min(starts) if starts else False
            request.actual_finish = max(active.mapped('date_finished')) if active and len(active) == len(request.line_ids) and all(mo.state == 'done' and mo.date_finished for mo in active) else False

    @api.model_create_multi
    def create(self, vals_list):
        audit = self._audit_fields()
        for vals in vals_list:
            if audit.intersection(vals) or vals.get('state', 'draft') != 'draft':
                raise UserError(_('Fabrication sign-offs are recorded by workflow actions.'))
            project = self.env['project.project'].browse(vals.get('project_id'))
            project.check_access('read')
            if not project or not project.company_id or not project.partner_id:
                raise UserError(_('Select a project with a company and customer before requesting fabrication.'))
            vals.update(name=self.env['ir.sequence'].next_by_code('acme.fabrication.request') or 'New', prepared_by_id=self.env.user.id, prepared_at=fields.Datetime.now())
        return super().create(vals_list)

    @api.model
    def _audit_fields(self):
        return {'name', 'prepared_by_id', 'prepared_at', 'approved_by_id', 'approved_at', 'received_by_id', 'received_at', 'reported_by_id', 'reported_at', 'accepted_by_id', 'accepted_at', 'actual_start', 'actual_finish', 'review_activity_id'}

    def _check_draft_editor(self):
        self.check_access('write')
        for request in self:
            if request.state != 'draft' or (request.prepared_by_id != self.env.user and not self.env.user.has_group('project.group_project_manager')):
                raise UserError(_('Only the preparer or a Project Manager may edit a draft fabrication request.'))

    def write(self, vals):
        if self.env.context.get('acme_fab_token') is not _FAB_TOKEN:
            if self._audit_fields().intersection(vals) or 'state' in vals or 'production_ids' in vals:
                raise UserError(_('Use the fabrication workflow actions for states and sign-offs.'))
            if set(vals) - {'message_main_attachment_id'}:
                if set(vals) == {'decision_note'} and all(r.state == 'submitted' and r.approver_id == self.env.user for r in self):
                    pass
                else:
                    self._check_draft_editor()
        return super().write(vals)

    def unlink(self):
        self._check_draft_editor()
        if self.mapped('production_ids'):
            raise UserError(_('A fabrication request with manufacturing orders cannot be deleted.'))
        return super().unlink()

    @api.constrains('drawing_pages', 'boq_pages', 'instruction_pages', 'completion_date', 'order_date')
    def _check_form(self):
        for request in self:
            if min(request.drawing_pages, request.boq_pages, request.instruction_pages) < 0:
                raise ValidationError(_('Attachment page counts cannot be negative.'))
            if request.completion_date < request.order_date:
                raise ValidationError(_('Completion date cannot precede the order date.'))

    def _lock(self):
        self.ensure_one()
        self.check_access('write')
        self.env.cr.execute('SELECT id FROM acme_fabrication_request WHERE id = %s FOR UPDATE', [self.id])
        self.invalidate_recordset()

    def action_submit(self):
        self._lock()
        self._check_draft_editor()
        if not self.line_ids:
            raise UserError(_('Add the requested finished products and their components.'))
        for person in (self.approver_id, self.receiver_id, self.acceptor_id):
            if not person or person.share or self.company_id not in person.company_ids:
                raise UserError(_('Configure internal approver, receiver and acceptor users in this company.'))
        if self.approver_id == self.prepared_by_id:
            raise UserError(_('The preparer cannot approve their own fabrication request.'))
        if not self.receiver_id.has_group('mrp.group_mrp_user'):
            raise UserError(_('The workshop receiver needs native Manufacturing access.'))
        if self.acceptor_id == self.receiver_id:
            raise UserError(_('Choose a completion acceptor different from the workshop receiver.'))
        for line in self.line_ids:
            line._validate_specification()
        self.with_context(acme_fab_token=_FAB_TOKEN).write({'state': 'submitted'})
        activity = self.activity_schedule('mail.mail_activity_data_todo', user_id=self.approver_id.id, summary=_('Review fabrication request %s', self.name))
        self.with_context(acme_fab_token=_FAB_TOKEN).write({'review_activity_id': activity.id})

    def action_approve(self):
        self._lock()
        if self.state != 'submitted' or self.approver_id != self.env.user or self.prepared_by_id == self.env.user:
            raise UserError(_('Only the assigned independent approver may approve this submitted request.'))
        self.with_context(acme_fab_token=_FAB_TOKEN).write({'state': 'approved', 'approved_by_id': self.env.user.id, 'approved_at': fields.Datetime.now()})
        if self.review_activity_id:
            self.review_activity_id.action_feedback(feedback=_('Fabrication request approved.'))

    def action_reject(self):
        self._lock()
        if self.state != 'submitted' or self.approver_id != self.env.user or not (self.decision_note or '').strip():
            raise UserError(_('The assigned approver must enter a rejection reason.'))
        self.with_context(acme_fab_token=_FAB_TOKEN).write({'state': 'rejected'})
        if self.review_activity_id:
            self.review_activity_id.action_feedback(feedback=self.decision_note)

    def action_receive(self):
        self._lock()
        if self.state != 'approved' or self.receiver_id != self.env.user:
            raise UserError(_('Only the assigned workshop receiver may receive an approved request.'))
        for line in self.line_ids:
            line._ensure_native_bom()
        self.with_context(acme_fab_token=_FAB_TOKEN).write({'state': 'received', 'received_by_id': self.env.user.id, 'received_at': fields.Datetime.now()})

    def action_report_completion(self):
        self._lock()
        if self.state != 'received' or (self.env.user != self.receiver_id and not self.env.user.has_group('mrp.group_mrp_manager')):
            raise UserError(_('The workshop must report completion after receiving the request.'))
        for line in self.line_ids:
            production = line.production_ids.filtered(lambda mo: mo.state != 'cancel')
            if len(production) != 1 or production.state != 'done' or production.product_uom_id._compute_quantity(production.qty_produced, line.uom_id) + 1e-6 < line.quantity:
                raise UserError(_('Every requested product must have a completed manufacturing order for its full requested quantity.'))
        self.with_context(acme_fab_token=_FAB_TOKEN).write({'state': 'reported', 'reported_by_id': self.env.user.id, 'reported_at': fields.Datetime.now()})

    def action_accept(self):
        self._lock()
        if self.state != 'reported' or self.acceptor_id != self.env.user or self.reported_by_id == self.env.user:
            raise UserError(_('The assigned acceptor must independently accept the reported completed work.'))
        self.with_context(acme_fab_token=_FAB_TOKEN).write({'state': 'accepted', 'accepted_by_id': self.env.user.id, 'accepted_at': fields.Datetime.now()})

    def action_cancel(self):
        self._lock()
        if self.state not in ('draft', 'submitted', 'approved', 'received') or self.prepared_by_id != self.env.user or self.production_ids.filtered(lambda mo: mo.state != 'cancel'):
            raise UserError(_('The preparer may cancel an unfinished request only when it has no active manufacturing orders.'))
        self.with_context(acme_fab_token=_FAB_TOKEN).write({'state': 'cancel'})

    def action_manufacture(self):
        self.ensure_one()
        if self.state != 'received':
            raise UserError(_('Receive the approved request before creating manufacturing orders.'))
        return {'type': 'ir.actions.act_window', 'res_model': 'mrp.production', 'view_mode': 'list,form', 'domain': [('acme_fabrication_request_id', '=', self.id)], 'context': {'default_acme_fabrication_request_id': self.id}}

    def action_print(self):
        return self.env.ref('acme_workshop_fabrication.report_fabrication').report_action(self)


class FabricationLine(models.Model):
    _name = 'acme.fabrication.line'
    _description = 'Requested Fabricated Product and BoM'
    _check_company_auto = True

    request_id = fields.Many2one('acme.fabrication.request', required=True, ondelete='cascade', check_company=True)
    company_id = fields.Many2one(related='request_id.company_id', store=True)
    product_id = fields.Many2one('product.product', required=True, check_company=True, domain="[('type', '=', 'consu')]")
    name = fields.Char(string='Description / Specification', required=True)
    quantity = fields.Float(default=1, required=True)
    uom_id = fields.Many2one('uom.uom', string='Unit', required=True)
    remark = fields.Text()
    component_ids = fields.One2many('acme.fabrication.component', 'line_id', string='Components for Requested Batch', copy=True)
    bom_id = fields.Many2one('mrp.bom', readonly=True, copy=False, check_company=True)
    production_ids = fields.One2many('mrp.production', 'acme_fabrication_line_id', copy=False)

    @api.onchange('product_id')
    def _onchange_product(self):
        if self.product_id:
            self.uom_id = self.product_id.uom_id
            self.name = self.product_id.display_name

    def _validate_specification(self):
        for line in self:
            if not math.isfinite(line.quantity) or line.quantity <= 0 or line.product_id.type != 'consu' or not line.component_ids:
                raise ValidationError(_('A fabricated goods product, positive quantity and component list are required.'))
            line.uom_id._compute_quantity(line.quantity, line.product_id.uom_id)
            for component in line.component_ids:
                component._check_component()

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            self.env['acme.fabrication.request'].browse(vals.get('request_id'))._check_draft_editor()
            if vals.get('bom_id') or vals.get('production_ids'):
                raise UserError(_('Native manufacturing links are system generated.'))
        return super().create(vals_list)

    def write(self, vals):
        if self.env.context.get('acme_fab_token') is not _FAB_TOKEN:
            self.mapped('request_id')._check_draft_editor()
            if {'bom_id', 'production_ids', 'request_id'}.intersection(vals):
                raise UserError(_('A fabrication line cannot be moved or assigned manufacturing links manually.'))
        return super().write(vals)

    def unlink(self):
        self.mapped('request_id')._check_draft_editor()
        return super().unlink()

    def _ensure_native_bom(self):
        self.ensure_one()
        if not self.bom_id:
            self._validate_specification()
            bom = self.env['mrp.bom'].with_context(acme_fab_token=_FAB_TOKEN).create({'acme_request_line_id': self.id, 'product_tmpl_id': self.product_id.product_tmpl_id.id, 'product_id': self.product_id.id,
                'company_id': self.company_id.id, 'type': 'normal', 'product_qty': self.quantity, 'product_uom_id': self.uom_id.id,
                'code': '%s / %s' % (self.request_id.name, self.id),
                'bom_line_ids': [Command.create({'product_id': part.product_id.id, 'product_qty': part.quantity, 'product_uom_id': part.uom_id.id}) for part in self.component_ids]})
            self.with_context(acme_fab_token=_FAB_TOKEN).write({'bom_id': bom.id})
        return self.bom_id


class FabricationComponent(models.Model):
    _name = 'acme.fabrication.component'
    _description = 'Fabrication Request Component'
    _check_company_auto = True

    line_id = fields.Many2one('acme.fabrication.line', required=True, ondelete='cascade', check_company=True)
    company_id = fields.Many2one(related='line_id.company_id', store=True)
    product_id = fields.Many2one('product.product', required=True, check_company=True, domain="[('type', '=', 'consu')]")
    quantity = fields.Float(default=1, required=True, help='Component quantity for the entire requested output batch, not per unit.')
    acme_material_classification = fields.Selection([
        ('supplied_installed', 'Supplied and installed'),
        ('consumable', 'Consumable'),
    ], string='Material Classification')
    uom_id = fields.Many2one('uom.uom', required=True, string='Unit')

    @api.onchange('product_id')
    def _onchange_product(self):
        if self.product_id:
            self.uom_id = self.product_id.uom_id

    @api.constrains('quantity', 'product_id', 'uom_id')
    def _check_component(self):
        for component in self:
            if not math.isfinite(component.quantity) or component.quantity <= 0 or component.product_id.type != 'consu':
                raise ValidationError(_('Component materials must be goods with positive quantities.'))
            component.uom_id._compute_quantity(component.quantity, component.product_id.uom_id)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            self.env['acme.fabrication.line'].browse(vals.get('line_id')).request_id._check_draft_editor()
        return super().create(vals_list)

    def write(self, vals):
        self.mapped('line_id.request_id')._check_draft_editor()
        if 'line_id' in vals:
            raise UserError(_('Component lines cannot be moved between requests.'))
        return super().write(vals)

    def unlink(self):
        self.mapped('line_id.request_id')._check_draft_editor()
        return super().unlink()
