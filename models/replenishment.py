from odoo import api, Command, fields, models, _
from odoo.exceptions import AccessError, UserError
from odoo.addons.acme_project_core.models.boq_revision import _BOQ_TOKEN
from odoo.addons.stock.models.stock_rule import Procurement


_BOQ_FABRICATION = object()


class ProjectReplenishment(models.Model):
    _inherit = 'project.project'

    acme_staging_location_id = fields.Many2one(
        'stock.location', string='Project Site Stock Location', check_company=True,
        domain="[('usage', '=', 'internal'), ('company_id', '=', company_id)]",
    )

    def write(self, vals):
        if 'acme_staging_location_id' in vals:
            self._acme_check_preparation_editor(allow_dm=True)
            if self.acme_boq_ids.acme_demand_move_ids.filtered(lambda move: move.state != 'cancel'):
                raise UserError(_('Keep the staging location while BOQ transfers are active or completed.'))
        return super().write(vals)

    acme_can_confirm_boq = fields.Boolean(compute='_compute_acme_can_confirm_boq')

    @api.depends('user_id', 'company_id', 'acme_boq_initialized')
    @api.depends_context('uid', 'company')
    def _compute_acme_can_confirm_boq(self):
        edm = self.env.user.has_group('acme_project_core.group_acme_edm')
        for project in self:
            project.acme_can_confirm_boq = bool(project.acme_boq_initialized and project.company_id in self.env.companies
                                               and (edm or project.user_id == self.env.user))

    def action_acme_replenish_boq(self):
        self.ensure_one()
        self.check_access('write')
        if self.company_id not in self.env.companies or not (
            self.user_id == self.env.user or self.env.user.has_group('acme_project_core.group_acme_edm')
        ):
            raise AccessError(_('Only the assigned PM or an authorized EDM in the project company can confirm the BOQ.'))
        if not self.env.user.has_group('stock.group_stock_user'):
            raise AccessError(_('Inventory access is required to confirm the BOQ.'))
        self.env.cr.execute('UPDATE project_project SET id = id WHERE id = %s', [self.id])
        sources = self.env['acme.fabrication.line'].search([('request_id.project_id', '=', self.id)])
        if sources:
            self.env.cr.execute(
                'SELECT id FROM acme_fabrication_line WHERE id IN %s ORDER BY id FOR SHARE',
                [tuple(sources.ids)],
            )
        self.env.invalidate_all()
        order = self.acme_order_id
        warehouse = order.warehouse_id
        if (not self.acme_boq_initialized or order.state != 'sale' or order.company_id != self.company_id
                or not self.account_id or not warehouse or warehouse.company_id != self.company_id):
            raise UserError(_('Confirm the contract order and configure its Main Warehouse, project analytic account, and approved BOQ first.'))
        messages = []
        for line in self.acme_boq_ids:
            issues = line._acme_confirm_demand(warehouse, order)
            line.with_context(acme_boq_token=_BOQ_TOKEN).acme_sourcing_issue = '\n'.join(issues) or False
            messages.extend('%s: %s' % (line.display_name, issue) for issue in issues)
        action = self._acme_sourcing_review_action()
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {'title': _('Confirm BOQ'), 'type': 'warning' if messages else 'success',
                       'message': '\n'.join(messages) if messages else _('Effective BOQ demand reviewed and replenished.'),
                       'sticky': bool(messages), 'next': action},
        }


class ProjectBoqReplenishment(models.Model):
    _inherit = 'acme.project.boq'

    acme_demand_move_ids = fields.One2many('stock.move', 'acme_boq_demand_id', string='BOQ Demand Transfers')
    acme_direct_purchase_ids = fields.One2many('purchase.order', 'acme_boq_direct_demand_id')
    acme_demand_fabrication_line_ids = fields.One2many('acme.fabrication.line', 'acme_boq_demand_id')
    acme_sourcing_issue = fields.Text(string='Sourcing Blockers', readonly=True, copy=False)
    acme_sourcing_rfqs = fields.Many2many('purchase.order', compute='_compute_acme_sourcing_documents', string='Purchases / RFQs')
    acme_sourcing_transfers = fields.Many2many('stock.picking', compute='_compute_acme_sourcing_documents', string='Transfers')
    acme_sourcing_fabrication_requests = fields.Many2many(
        'acme.fabrication.request', compute='_compute_acme_sourcing_documents', string='Fabrication Requests',
    )

    def _compute_acme_sourcing_documents(self):
        for line in self:
            moves = line.acme_demand_move_ids
            line.acme_sourcing_rfqs = moves.created_purchase_line_ids.order_id | line.acme_direct_purchase_ids
            line.acme_sourcing_transfers = moves.picking_id
            line.acme_sourcing_fabrication_requests = line.acme_demand_fabrication_line_ids.request_id

    def _acme_fabrication_coverage(self, product):
        return sum(line.uom_id._compute_quantity(line.quantity, product.uom_id, round=False)
                   for line in self.acme_demand_fabrication_line_ids
                   if line.product_id == product and line.request_id.state not in ('cancel', 'rejected'))

    def _acme_direct_purchase_coverage(self, product):
        lines = self.acme_direct_purchase_ids.filtered(lambda order: order.state != 'cancel').order_line
        return sum(line.product_uom_id._compute_quantity(line.product_qty, product.uom_id, round=False)
                   for line in lines if line.product_id == product)

    def _acme_confirm_demand(self, warehouse, order):
        try:
            demand = self._acme_component_demand()
            if self.source_line_id.order_id != order or self.source_line_id.move_ids.filtered(
                lambda move: move.state != 'cancel'
            ):
                raise UserError(_('The source must belong to this contract and have no Sales Order stock moves. Review the commercial line to avoid duplicate demand.'))
        except UserError as error:
            return [str(error)]
        issues = []
        products = demand.keys() | set(self.acme_demand_move_ids.product_id | self.acme_demand_fabrication_line_ids.product_id | self.acme_direct_purchase_ids.order_line.product_id)
        for product in sorted(products, key=lambda item: item.id):
            if product.type != 'consu':
                continue
            try:
                with self.env.cr.savepoint():
                    warning = self._acme_confirm_product(product, demand.get(product, 0), warehouse)
                if warning:
                    issues.append('%s: %s' % (product.display_name, warning))
            except UserError as error:
                issues.append('%s: %s' % (product.display_name, error))
        return issues

    def _acme_confirm_product(self, product, quantity, warehouse):
        uom = product.uom_id
        required = uom.round(quantity, rounding_method='UP')
        fabrication = self._acme_fabrication_coverage(product)
        moves = self.acme_demand_move_ids.filtered(lambda move: move.product_id == product)
        transferred = sum(sum(self._acme_move_coverage(move)[:3]) for move in moves)
        direct_purchase = self._acme_direct_purchase_coverage(product)
        committed = fabrication + transferred + direct_purchase
        if uom.compare(committed, required) > 0:
            return _('Committed quantity %(committed)s exceeds revised demand %(required)s. Review existing supply.',
                     committed=committed, required=required)
        if not required:
            return
        rule_model = self.env['stock.rule'].with_company(self.company_id)
        routes = self.acme_sourcing_route_id or (product.route_ids | product.categ_id.total_route_ids)
        values = {'warehouse_id': warehouse, 'route_ids': routes, 'date_planned': fields.Datetime.now()}
        rule = rule_model._get_rule(product, warehouse.lot_stock_id, values) if routes else rule_model
        if rule and rule.route_id not in routes:
            rule = rule_model
        if routes and not rule:
            raise UserError(_('The configured route has no active rule at Main Warehouse stock. Check the warehouse and product routes.'))

        uncovered = required - committed
        if rule.action == 'manufacture':
            if uom.compare(uncovered, 0) > 0:
                self._acme_create_demand_fabrication(product, uncovered, rule, values)
            return
        if rule.action == 'buy':
            seller = rule._get_matching_supplier(product, uncovered if uom.compare(uncovered, 0) > 0 else required, uom, self.company_id, values)
            if uom.compare(uncovered, 0) <= 0:
                return
            if not seller:
                raise UserError(_('No eligible vendor is configured for the uncovered quantity. Check the product vendor, company, dates and minimum quantity.'))
            values.update(acme_boq_procurement=True, acme_boq_direct_demand_id=self.id,
                          route_ids=rule.route_id, project_id=self.project_id.id)
            rule_model.run([Procurement(product, uncovered, uom, warehouse.lot_stock_id, product.display_name,
                                        self.project_id.acme_order_id.name, self.company_id, values)])
            self.env.flush_all()
            self.invalidate_recordset(['acme_direct_purchase_ids'])
            if uom.compare(self._acme_direct_purchase_coverage(product) - direct_purchase, uncovered) < 0:
                raise UserError(_('The configured purchase or subcontract rule did not link the shortage to a project RFQ.'))
            return
        if not rule or (rule.action == 'pull' and rule.procure_method == 'make_to_stock'):
            return _('Stock only. Confirm BOQ created no sourcing document or reservation.')
        raise UserError(_('The configured route cannot supply this item. Check the Stock, Purchase, Manufacture, or subcontract rule.'))

    def _acme_create_demand_fabrication(self, product, quantity, rule, values):
        source = self.acme_sourcing_fabrication_line_id
        bom = source.bom_id if source.bom_id.product_id == product else self.env['mrp.bom']
        bom = bom or rule._get_matching_bom(product, self.company_id, values)
        if (not bom or not bom.active or bom.type != 'normal' or bom.product_qty <= 0
                or (bom.acme_request_line_id and (bom.acme_request_line_id.request_id.project_id != self.project_id
                    or bom.acme_request_line_id.request_id.state not in ('received', 'reported', 'accepted')
                    or bom.acme_request_line_id.bom_id != bom))):
            raise UserError(_('Configure a current approved manufacturing BoM for this project and product.'))
        factor = product.uom_id._compute_quantity(quantity, bom.product_uom_id, round=False) / bom.product_qty
        components = [Command.create({'product_id': part.product_id.id, 'uom_id': part.product_uom_id.id,
                                      'quantity': part.product_qty * factor})
                      for part in bom.bom_line_ids if not part._skip_bom_line(product)]
        if not components:
            raise UserError(_('The applicable manufacturing BoM has no components for this product variant.'))
        project = self.project_id
        today = fields.Date.context_today(self)
        self.env['acme.fabrication.request'].with_context(acme_boq_fabrication=_BOQ_FABRICATION).create({
            'project_id': project.id, 'title': _('%(product)s — BOQ %(line)s', product=product.display_name, line=self.display_name),
            'description': _('Uncovered fabrication demand from %s.', self.display_name),
            'completion_date': max(today, project.date or today),
            'line_ids': [Command.create({
                'product_id': product.id, 'name': product.display_name, 'quantity': quantity,
                'uom_id': product.uom_id.id, 'component_ids': components,
                'acme_boq_demand_id': self.id, 'acme_source_bom_id': bom.id,
            })],
        })

    def _acme_check_quantity_reduction(self, quantity):
        super()._acme_check_quantity_reduction(quantity)
        if not (self.acme_demand_move_ids or self.acme_demand_fabrication_line_ids or self.acme_direct_purchase_ids):
            return
        demand = self._acme_component_demand(quantity=quantity)
        products = self.acme_demand_move_ids.product_id | self.acme_demand_fabrication_line_ids.product_id | self.acme_direct_purchase_ids.order_line.product_id
        for product in products:
            moves = self.acme_demand_move_ids.filtered(lambda move: move.product_id == product)
            committed = (self._acme_fabrication_coverage(product) + self._acme_direct_purchase_coverage(product)
                         + sum(sum(self._acme_move_coverage(move)[:3]) for move in moves))
            proposed = product.uom_id.round(demand.get(product, 0), rounding_method='UP')
            if product.uom_id.compare(proposed, committed) < 0:
                raise UserError(_('The variation leaves %(product)s below existing reservations and supply. Resolve the commitment before authorization.',
                                  product=product.display_name))

    def _acme_move_coverage(self, move):
        product = move.product_id
        uom = product.uom_id
        convert = lambda record, amount: record.product_uom._compute_quantity(amount, uom, round=False)
        if move.state == 'done':
            return convert(move, move.quantity), 0, 0, []
        if move.state == 'cancel':
            return 0, 0, 0, []
        reserved = convert(move, move.quantity) if product.is_storable else 0
        upstream = move.move_orig_ids.filtered(lambda supply: supply.state not in ('done', 'cancel'))
        incoming = sum(convert(supply, supply.product_uom_qty) for supply in upstream)
        dates = upstream.mapped('date')
        quotes = move.created_purchase_line_ids.filtered(lambda line: line.state in ('draft', 'sent', 'to approve'))
        incoming += sum(line.product_uom_id._compute_quantity(line.product_qty, uom, round=False) for line in quotes)
        dates += quotes.mapped('date_planned')
        production = move.created_production_id
        if production.state not in (False, 'done', 'cancel') and not move.move_orig_ids.filtered(
            lambda supply: supply.production_id == production and supply.state != 'cancel'
        ):
            incoming += production.product_uom_id._compute_quantity(production.product_qty, uom, round=False)
            dates.append(production.date_finished)
        return 0, reserved, incoming, [date for date in dates if date]

class FabricationLineDemand(models.Model):
    _inherit = 'acme.fabrication.line'

    acme_boq_demand_id = fields.Many2one(
        'acme.project.boq', string='BOQ Demand', check_company=True, ondelete='restrict', readonly=True, copy=False,
    )
    acme_source_bom_id = fields.Many2one(
        'mrp.bom', string='Approved Source BoM', check_company=True, ondelete='restrict', readonly=True, copy=False,
    )

    @api.model_create_multi
    def create(self, vals_list):
        links = ('acme_boq_demand_id', 'acme_source_bom_id')
        if self.env.context.get('acme_boq_fabrication') is not _BOQ_FABRICATION and any(
            vals.get(field) or self.env.context.get('default_' + field) for vals in vals_list for field in links
        ):
            raise AccessError(_('Create BOQ demand fabrication through Confirm BOQ.'))
        return super().create(vals_list)

    def write(self, vals):
        if {'acme_boq_demand_id', 'acme_source_bom_id'}.intersection(vals) or (
            {'product_id', 'quantity', 'uom_id', 'component_ids'}.intersection(vals) and self.filtered('acme_boq_demand_id')
        ):
            raise UserError(_('Keep the approved BoM and quantities linked to the BOQ demand.'))
        return super().write(vals)

    def _ensure_native_bom(self):
        self.ensure_one()
        if self.acme_source_bom_id and not self.bom_id:
            from .fabrication import _FAB_TOKEN

            self.with_context(acme_fab_token=_FAB_TOKEN).bom_id = self.acme_source_bom_id
        return super()._ensure_native_bom()


class StockMove(models.Model):
    _inherit = 'stock.move'

    acme_boq_demand_id = fields.Many2one(
        'acme.project.boq', string='BOQ Demand', readonly=True, index=True, ondelete='restrict', check_company=True,
    )

    def _prepare_merge_moves_distinct_fields(self):
        return super()._prepare_merge_moves_distinct_fields() + ['acme_boq_demand_id']

    def _get_new_picking_values(self):
        values = super()._get_new_picking_values()
        projects = self.acme_boq_demand_id.project_id
        if projects and len(projects) == 1:
            values['project_id'] = projects.id
        return values


class PurchaseOrder(models.Model):
    _inherit = 'purchase.order'

    acme_boq_direct_demand_id = fields.Many2one(
        'acme.project.boq', string='BOQ Purchase Demand', check_company=True,
        readonly=True, copy=False, index=True, ondelete='restrict',
    )

    acme_boq_supply_move_id = fields.Many2one(
        'stock.move', string='BOQ Demand Transfer', check_company=True,
        readonly=True, copy=False, index=True, ondelete='restrict',
    )


class StockRule(models.Model):
    _inherit = 'stock.rule'

    def _make_po_get_domain(self, company_id, values, partner):
        domain = super()._make_po_get_domain(company_id, values, partner)
        if values.get('acme_boq_procurement'):
            domain += (('project_id', '=', values['project_id']),
                       ('acme_boq_supply_move_id', '=', values.get('acme_boq_supply_move_id') or False),
                       ('acme_boq_direct_demand_id', '=', values.get('acme_boq_direct_demand_id') or False))
            return domain
        return domain + (('acme_boq_supply_move_id', '=', False), ('acme_boq_direct_demand_id', '=', False))

    def _prepare_purchase_order(self, company_id, origins, values):
        result = super()._prepare_purchase_order(company_id, origins, values)
        if values[0].get('acme_boq_procurement'):
            result['project_id'] = values[0]['project_id']
            result['acme_boq_supply_move_id'] = values[0].get('acme_boq_supply_move_id')
            result['acme_boq_direct_demand_id'] = values[0].get('acme_boq_direct_demand_id')
        return result

    def _make_mo_get_domain(self, procurement, bom):
        domain = super()._make_mo_get_domain(procurement, bom)
        if procurement.values.get('acme_boq_procurement'):
            return domain + (('id', '=', False),)
        return domain
