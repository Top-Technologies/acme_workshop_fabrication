from odoo import Command
from odoo.tests.common import new_test_user, tagged
from odoo.exceptions import UserError
from odoo.addons.acme_project_core.tests.test_project_flow import ProjectFlowCase

@tagged('post_install', '-at_install')
class TestFabrication(ProjectFlowCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fab_approver = new_test_user(cls.env, login='fab_approver', groups='project.group_project_manager')
        cls.workshop = new_test_user(cls.env, login='fab_workshop', groups='mrp.group_mrp_manager,project.group_project_manager')
        cls.finished = cls.env['product.product'].create({'name': 'Fabricated bracket', 'type': 'consu'})
        cls.material = cls.env['product.product'].create({'name': 'Steel plate', 'type': 'consu'})

    def _request(self):
        return self.env['acme.fabrication.request'].create({'title': 'Brackets', 'project_id': self.project.id, 'description': 'Fabricate mounting brackets', 'order_date': '2026-11-18', 'completion_date': '2026-11-30', 'approver_id': self.fab_approver.id, 'receiver_id': self.workshop.id, 'acceptor_id': self.fab_approver.id, 'line_ids': [Command.create({'product_id': self.finished.id, 'name': 'Bracket specification', 'quantity': 2, 'uom_id': self.finished.uom_id.id, 'component_ids': [Command.create({'product_id': self.material.id, 'quantity': 3, 'uom_id': self.material.uom_id.id})]})]})

    def _receive(self, request):
        request.action_submit()
        request.with_user(self.fab_approver).action_approve()
        request.with_user(self.workshop).action_receive()

    def test_project_selector_keeps_readable_allowed_company_projects(self):
        other_company = self.env['res.company'].create({'name': 'Fabrication Project Company'})
        self.env.user.write({'company_ids': [Command.link(other_company.id)]})
        other_project = self.env['project.project'].create({
            'name': 'Cross-company fabrication project',
            'company_id': other_company.id,
            'partner_id': self.env['res.partner'].create({'name': 'Fabrication Customer'}).id,
        })
        Fabrication = self.env['acme.fabrication.request'].with_context(
            allowed_company_ids=self.env.user.company_ids.ids,
        )

        self.assertFalse(Fabrication._fields['project_id'].check_company)
        self.assertIn(
            other_project,
            self.env['project.project'].with_context(
                allowed_company_ids=self.env.user.company_ids.ids,
            ).search([]),
        )
        action = other_project.action_acme_fabrication()
        self.assertEqual(action['context']['default_project_id'], other_project.id)
        self.assertEqual(
            Fabrication.with_context(**action['context']).default_get(['project_id'])['project_id'],
            other_project.id,
        )

    def test_fabrication_generates_batch_bom_and_mo(self):
        request = self._request()
        self._receive(request)
        line = request.line_ids
        self.assertEqual(line.bom_id.product_qty, 2)
        self.assertEqual(line.bom_id.bom_line_ids.product_qty, 3)
        mo = self.env['mrp.production'].with_user(self.workshop).create({'acme_fabrication_request_id': request.id})
        self.assertEqual(mo.product_id, self.finished)
        self.assertEqual(mo.product_qty, 2)
        self.assertEqual(mo.bom_id, line.bom_id)
        self.assertEqual(mo.acme_project_id, self.project)
        self.assertFalse(request.actual_finish)

    def test_two_user_fabrication_lifecycle_and_report(self):
        preparer = new_test_user(
            self.env, login='fabrication_preparer', groups='project.group_project_user',
        )
        workshop = new_test_user(
            self.env, login='fabrication_workshop', groups='mrp.group_mrp_user',
        )
        request = self.env['acme.fabrication.request'].with_user(preparer).create({
            'title': 'Two-user brackets',
            'project_id': self.project.id,
            'description': 'Fabricate mounting brackets',
            'order_date': '2026-11-20',
            'completion_date': '2026-11-30',
            'prototype': 'yes',
            'drawing_status': 'full',
            'boq_status': 'partial',
            'instruction_status': 'full',
            'approver_id': workshop.id,
            'receiver_id': workshop.id,
            'acceptor_id': preparer.id,
            'line_ids': [Command.create({
                'product_id': self.finished.id,
                'name': 'Bracket specification',
                'quantity': 2,
                'uom_id': self.finished.uom_id.id,
                'component_ids': [Command.create({
                    'product_id': self.material.id,
                    'quantity': 3,
                    'uom_id': self.material.uom_id.id,
                })],
            })],
        })
        request.with_user(preparer).action_submit()
        request.with_user(workshop).action_approve()
        request.with_user(workshop).action_receive()
        line = request.line_ids
        self.assertTrue(line.bom_id)

        action = request.action_manufacture()
        defaults = self.env['mrp.production'].with_user(workshop).with_context(
            **action['context'],
        ).default_get([
            'acme_fabrication_request_id', 'acme_fabrication_line_id', 'product_id',
            'product_qty', 'product_uom_id', 'bom_id', 'origin', 'project_id',
        ])
        self.assertEqual(defaults['product_id'], line.product_id.id)
        self.assertEqual(defaults['product_qty'], line.quantity)
        self.assertEqual(defaults['bom_id'], line.bom_id.id)

        mo = self.env['mrp.production'].with_user(workshop).create({
            'acme_fabrication_request_id': request.id,
        })
        self.assertEqual(mo.product_id, line.product_id)
        self.assertEqual(mo.product_qty, line.quantity)
        self.assertEqual(mo.product_uom_id, line.uom_id)
        self.assertEqual(mo.bom_id, line.bom_id)
        self.assertEqual(mo.acme_project_id, request.project_id)
        if 'project_id' in mo._fields:
            self.assertEqual(mo.project_id, request.project_id)

        mo.action_confirm()
        mo.button_mark_done()
        self.assertEqual(mo.state, 'done')
        self.assertTrue(request.actual_start)
        self.assertTrue(request.actual_finish)
        request.with_user(workshop).action_report_completion()
        request.with_user(preparer).action_accept()
        self.assertEqual(request.state, 'accepted')
        pdf, _ = self.env.ref('acme_workshop_fabrication.report_fabrication')._render_qweb_pdf(request.ids)
        self.assertTrue(pdf)

    def test_single_line_request_prefills_mo_onchange_and_default(self):
        request = self._request()
        self._receive(request)
        line = request.line_ids
        production = self.env['mrp.production'].new({
            'acme_fabrication_request_id': request.id,
        })
        production._onchange_acme_request()
        self.assertEqual(production.acme_fabrication_line_id, line)
        self.assertEqual(production.product_id, line.product_id)
        self.assertEqual(production.product_qty, line.quantity)
        self.assertEqual(production.product_uom_id, line.uom_id)
        self.assertEqual(production.bom_id, line.bom_id)
        self.assertEqual(production.origin, request.name)
        defaults = self.env['mrp.production'].with_context(
            default_acme_fabrication_request_id=request.id,
        ).default_get([
            'acme_fabrication_request_id', 'acme_fabrication_line_id',
            'product_id', 'product_uom_id', 'product_qty', 'bom_id', 'origin',
        ])
        self.assertEqual(defaults['acme_fabrication_line_id'], line.id)
        self.assertEqual(defaults['product_id'], line.product_id.id)
        self.assertEqual(defaults['bom_id'], line.bom_id.id)

    def test_fabrication_self_approval_and_forged_signoffs(self):
        request = self._request()
        request.approver_id = self.env.user
        with self.assertRaises(UserError):
            request.action_submit()
        with self.assertRaises(UserError):
            request.write({'state': 'accepted', 'accepted_by_id': self.env.user.id})

    def test_receiver_cannot_also_accept_and_unreceived_request_cannot_make_mo(self):
        request = self._request()
        request.acceptor_id = request.receiver_id
        with self.assertRaises(UserError):
            request.action_submit()
        with self.assertRaises(UserError):
            self.env['mrp.production'].with_user(self.workshop).create({
                'acme_fabrication_request_id': request.id,
            })

    def test_mo_selector_disables_quick_create(self):
        arch = self.env.ref(
            'acme_workshop_fabrication.mrp_fabrication_selector',
        ).get_combined_arch()
        self.assertIn("'no_create': True", arch)
        self.assertIn("'no_create_edit': True", arch)

    def test_approved_request_component_and_bom_are_locked(self):
        request = self._request()
        self._receive(request)
        with self.assertRaises(UserError):
            request.line_ids.component_ids.write({'quantity': 9})
        with self.assertRaises(UserError):
            request.line_ids.bom_id.write({'product_qty': 9})
        with self.assertRaises(UserError):
            request.line_ids.bom_id.bom_line_ids.write({'product_qty': 9})

    def test_fabrication_cannot_complete_without_native_production(self):
        request = self._request()
        self._receive(request)
        with self.assertRaises(UserError):
            request.with_user(self.workshop).action_report_completion()

    def test_mo_cannot_override_approved_specification(self):
        request = self._request()
        self._receive(request)
        with self.assertRaises(UserError), self.cr.savepoint():
            self.env['mrp.production'].with_user(self.workshop).create({'acme_fabrication_request_id': request.id, 'product_qty': 100})
